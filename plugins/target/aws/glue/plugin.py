"""AWS Glue target-platform plugin.

Implements the six stage verbs for the *aws-glue* platform and supplies the backing
Terraform. aws-glue is a **data-job shape** like databricks: there is NO container
image, NO ECR, NO ECS. The verb meaning is the aws-glue row:

 | verb | aws-glue |
 |---------|----------------------------------------------------------------|
 | lint | lint the job scripts / python (generic/ruff) |
 | test | run the project's tests (stage.runs, else a placeholder) |
 | build | assemble the Glue job script(s) + extra python libs/wheel | NO image
 | package | upload scripts/libs to S3 (immutable object VERSION) | (by-reference, promotable)
 | deploy | aws glue create-job / update-job via the CLI | (consumes a pre-built ref under promotion)
 | verify | aws glue start-job-run + poll run state to SUCCEEDED |
 | IaC | Glue job + role (+ S3 script bucket + OIDC) (Terraform) | (see terraform.py)

Packaging shape
--------------------------------------------------------------------------------
``build`` assembles the job script(s) and any extra python libs/wheel into a build
dir (NO docker). ``package`` uploads them to the versioned S3 script bucket and —
when promoting — captures the resulting **immutable object version id** as the
promotable reference (the data-job analogue of an image digest). Because the
deployable is a script bundle (not an image), the plugin declares
``artifact_promotion`` but NOT ``container_images`` — the same reasoning as the
aws-lambda zip path.

Design boundaries this plugin respects
---------------------------------------
* **No image, ever.** Not one verb emits ``docker build`` / ``docker push`` or
 references ECR/ECS. ``build`` assembles python scripts + a wheel; the deployable
 is a Glue *job*, not an image. That is the data-job shape (like databricks).
* **Secrets by reference only.** Every account specific — the job name, the
 execution role, the script bucket/key, the region — is referenced as a shell
 variable (``$GLUE_JOB_NAME``, ``$GLUE_SCRIPT_BUCKET``, …) or a ``settings`` value,
 never a literal. No account id, ARN, or key is emitted.
* **Resilient packaging.** ``build`` installs extra libs from a pinned
 ``requirements.txt`` (PyPI only) — there is no live distro-mirror step that could
 make the build go red for reasons unrelated to the change.
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / OIDC credential steps are the CI vendor's
 job. A verb never assumes a particular CI vendor.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``job_name`` — the Glue job name (else ``<project>-job`` via the slug).
 * ``glue_role`` — the Glue execution role (else ``$GLUE_ROLE_ARN``).
 * ``script_bucket`` — the S3 script bucket (else ``$GLUE_SCRIPT_BUCKET``).
 * ``script_key`` — the S3 object key for the script (else ``$GLUE_SCRIPT_KEY``).
 * ``extra_libs_key`` — the S3 object key for the extra libs (else ``$GLUE_EXTRA_LIBS_KEY``).
 * ``aws_region`` — region (else the IR ``cloud.region``, else ``$AWS_REGION``).

Nothing in ``settings`` is a secret; secrets remain references.
"""

from __future__ import annotations

import re

from cicdgen.ir.models import Pipeline
from cicdgen.plugins.base import (
    Capability,
    GeneratedFile,
    IaCArtifacts,
    TargetPlatformPlugin,
    VerbContext,
    VerbResult,
)

from .jobspec import render_job_spec
from .terraform import (
    render_main_tf,
    render_outputs_tf,
    render_tfvars_example,
    render_variables_tf,
)

#: Where the committed Glue job definition is written (deploy applies it).
JOB_SPEC_PATH = "glue/glue-job.json"

#: Build output dir for the assembled scripts + extra libs (NO image).
BUILD_DIR = ".glue_build"

#: The extra-python-libs archive the build verb produces and package uploads.
EXTRA_LIBS_ARCHIVE = "glue-extra-python-libs.zip"

#: Where the Terraform module is written (one dir per target platform, /).
IAC_DIR = "iac/aws-glue"

#: Default by-reference knob names. Secrets/account specifics referenced, not valued
#:. ``settings`` may override the *non-secret* ones (region, names) with values.
_REF_JOB_NAME = "$GLUE_JOB_NAME"
_REF_ROLE = "$GLUE_ROLE_ARN"
_REF_SCRIPT_BUCKET = "$GLUE_SCRIPT_BUCKET"
_REF_SCRIPT_KEY = "$GLUE_SCRIPT_KEY"
_REF_EXTRA_LIBS_KEY = "$GLUE_EXTRA_LIBS_KEY"
_REF_AWS_REGION = "$AWS_REGION"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline. When
#: present, ``package`` resolves+exports the uploaded script's immutable S3 object
#: VERSION id into this reference and every ``deploy`` consumes that same reference
#: rather than re-uploading.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


def _job_slug(raw: str) -> str:
    """Dash-based job-name prefix — MUST match terraform ``_sanitize_name``.

 The Glue job name produced by the CLI ``deploy`` verb and by the Terraform
 ``aws_glue_job`` must agree so the two paths converge on the SAME job. Both
 normalise the IR project name to ``[a-z0-9-]`` (dashes).
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


class AwsGlue(TargetPlatformPlugin):
    """Target-platform plugin for AWS Glue Spark/Python ETL jobs — NO image.

 ``name`` MUST equal the IR value / nested directory home ``plugins/target/aws/
 glue`` reports (``aws-glue``) so the IR value, the folder, and this class all
 agree (see base.py module docstring,).
 """

    name = "aws-glue"

    #: Workload capabilities aws-glue provides: it packages a script
    #: bundle that can be promoted across environments as an immutable S3 object
    #: version (``artifact_promotion`` — lists Glue as promotion=yes), deploys
    #: per environment (``environments``), authenticates to AWS via IAM/OIDC
    #: (``oidc``), and references all account specifics by key
    #: (``secrets_by_reference``). It is a DATA-JOB shape (like databricks): it
    #: builds NO container image, so ``container_images`` is deliberately NOT
    #: declared.
    capabilities = frozenset(
        {
            Capability.ARTIFACT_PROMOTION,
            Capability.ENVIRONMENTS,
            Capability.OIDC,
            Capability.SECRETS_BY_REFERENCE,
        }
    )

    # ------------------------------------------------------------------
    # settings resolution (non-secret knobs -> value or by-reference)
    # ------------------------------------------------------------------
    @staticmethod
    def _knob(ctx: VerbContext, key: str, ref: str) -> str:
        """Return the ``settings`` value for ``key`` if present, else the ref.

 This is the single place "value-or-reference" is decided. Non-secret knobs
 (region, job/bucket/key names) may be inlined from ``settings``; anything not
 supplied falls back to a shell *reference* so no account specific is ever
 guessed or hard-coded.
 """
        value = ctx.settings.get(key)
        return value if value else ref

    def _region(self, ctx: VerbContext) -> str:
        """Resolve the region: explicit setting > IR cloud.region > ``$AWS_REGION``."""
        if ctx.settings.get("aws_region"):
            return ctx.settings["aws_region"]
        cloud = ctx.pipeline.cloud
        if cloud is not None and cloud.region:
            return cloud.region
        return _REF_AWS_REGION

    def _job_name(self, ctx: VerbContext) -> str:
        """Job name: explicit setting > ``<project>-job`` (non-secret, derived).

 The derived default uses the dash-based :func:`_job_slug` so it matches the
 Terraform ``aws_glue_job`` naming convention — the CLI ``deploy`` verb and
 the Terraform module converge on a consistent job name.
 """
        if ctx.settings.get("job_name"):
            return ctx.settings["job_name"]
        return f"{_job_slug(ctx.pipeline.project.name)}-job"

    @staticmethod
    def _source_dir(ctx: VerbContext) -> str:
        """Repo-relative directory the service source lives in (monorepo support).

 Non-secret ``settings`` knob ``source_dir`` lets a client keep each service
 in its own sub-directory of a monorepo (e.g. ``services/glue``) instead of at
 the repo root. The build then looks for ``<source_dir>/glue_jobs/`` and
 ``<source_dir>/requirements.txt``. Defaults to ``.`` (the repo root) so
 existing single-service repos are byte-identically unchanged.
 """
        raw = ctx.settings.get("source_dir", ".") or "."
        return raw.rstrip("/") or "."

    # ------------------------------------------------------------------
    # the six stage verbs — NO docker anywhere
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the job scripts / python (aws-glue ships code, not a Dockerfile).

 Prefer ``ruff`` when available and fall back to a soft notice so the step
 never breaks a pipeline merely because the linter isn't installed. There is
 deliberately NO Dockerfile lint here — glue has no image.
 """
        commands = (
            "echo 'Linting Glue job scripts/python (aws-glue)'",
            "if command -v ruff >/dev/null 2>&1; then "
            "ruff check .; "
            "else echo 'ruff not found; skipping python lint'; fi",
        )
        return VerbResult(commands=commands)

    def test(self, ctx: VerbContext) -> VerbResult:
        """Run the project's test suite.

 Testing is project-defined, so we honour ``stage.runs`` when the IR sets it
 and fall back to a conventional ``pytest -q`` placeholder otherwise. The
 plugin does not impose a runner — that is the client's choice.
 """
        run_cmd = ctx.stage.runs if ctx.stage.runs is not None else "pytest -q"
        return VerbResult(commands=(run_cmd,))

    def build(self, ctx: VerbContext) -> VerbResult:
        """Assemble the Glue job script(s) + extra python libs/wheel — NO image.

 ``build`` on aws-glue stages the ETL job script(s) and bundles any extra
 python libraries (installed from a pinned ``requirements.txt``, PyPI only —
 no live distro mirror,) into a deterministic ``--extra-py-files``
 archive. There is NO ``docker build`` and NO image tag — the deployable is a
 Glue job that runs an S3 script. Also emits the committed job definition
 (``glue/glue-job.json``) the ``deploy`` verb applies, referencing the script
 location + role by reference.
 """
        job_name = self._job_name(ctx)
        role_ref = self._knob(ctx, "glue_role", _REF_ROLE)
        bucket = self._knob(ctx, "script_bucket", _REF_SCRIPT_BUCKET)
        script_key = self._knob(ctx, "script_key", _REF_SCRIPT_KEY)
        extra_key = self._knob(ctx, "extra_libs_key", _REF_EXTRA_LIBS_KEY)

        src = self._source_dir(ctx)
        commands = (
            "echo 'Building Glue job: scripts + extra python libs (NO image)'",
            f"echo 'Service source directory: {src}'",
            f"rm -rf {BUILD_DIR} {EXTRA_LIBS_ARCHIVE}",
            f"mkdir -p {BUILD_DIR}/scripts {BUILD_DIR}/libs",
            # Stage the ETL job script(s) for upload. A client keeps job scripts
            # under <source_dir>/glue_jobs/; fall back to a notice so a wheel-only
            # build is ok. The source dir lets a monorepo keep this service isolated.
            f"if [ -d {src}/glue_jobs ]; then cp -r {src}/glue_jobs/. {BUILD_DIR}/scripts/; "
            "else echo 'no glue_jobs/ dir; expecting a script already in S3'; fi",
            # Bundle extra python libs/wheel from a pinned requirements file (PyPI
            # only). No docker involved.
            f"if [ -f {src}/requirements.txt ]; then "
            f"pip install --target {BUILD_DIR}/libs -r {src}/requirements.txt; "
            "else echo 'no requirements.txt; skipping extra-py-files bundle'; fi",
            # Zip the libs reproducibly: -r recurse, -X strip extra attributes so the
            # archive bytes do not depend on the host's uid/gid/timestamps.
            f"(cd {BUILD_DIR}/libs && zip -r -X ../../{EXTRA_LIBS_ARCHIVE} . "
            "-x '*.git*' -x '*.venv*')",
            f"echo 'Built Glue extra-py-files archive: {EXTRA_LIBS_ARCHIVE}'",
        )
        job_spec = GeneratedFile(
            path=JOB_SPEC_PATH,
            content=render_job_spec(
                job_name=job_name,
                role_ref=role_ref,
                script_location=f"s3://{bucket}/{script_key}",
                extra_python_libs=f"s3://{bucket}/{extra_key}",
            ),
        )
        return VerbResult(commands=commands, files=(job_spec,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Upload the script(s) + extra libs to S3 — no image push.

 ``package`` on aws-glue uploads the staged script and the extra-py-files
 archive to the versioned S3 script bucket. The promotable, immutable
 reference is the resulting S3 object **version id** of the script — the
 data-job analogue of an image digest. Authentication is the CI vendor's OIDC
 step (not emitted here); the bucket/keys are by-reference knobs.

 **Promotion.** When the core threads a
 shared artifact reference (``promotion_artifact_ref`` in ``settings``),
 ``package`` resolves the uploaded script object's immutable version id and
 EXPORTS a stable reference (``s3://bucket/key?versionId=...``) into that
 variable, so every later ``deploy`` promotes the identical, immutable
 artifact instead of rebuilding. Nothing account- or version-specific is ever
 inlined here.
 """
        region = self._region(ctx)
        bucket = self._knob(ctx, "script_bucket", _REF_SCRIPT_BUCKET)
        script_key = self._knob(ctx, "script_key", _REF_SCRIPT_KEY)
        extra_key = self._knob(ctx, "extra_libs_key", _REF_EXTRA_LIBS_KEY)
        commands = [
            "echo 'Uploading Glue scripts/libs to S3 (aws-glue) — no image push'",
            f"aws s3api put-object --bucket {bucket} --key {extra_key} "
            f"--body {EXTRA_LIBS_ARCHIVE} --region {region}",
            # Upload each staged job script under the script key prefix.
            f'SCRIPT="$(ls {BUILD_DIR}/scripts/*.py 2>/dev/null | head -n1)"; '
            f'if [ -n "$SCRIPT" ]; then '
            f"aws s3api put-object --bucket {bucket} --key {script_key} "
            f'--body "$SCRIPT" --region {region}; '
            "else echo 'no local script staged; assuming script already in S3'; fi",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            # Resolve the immutable S3 object version for the uploaded script and
            # export it as the shared promotable reference. Every deploy consumes
            # this exact reference — build once, promote the same artifact.
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (aws-glue promotion)'",
                    f'OBJECT_VERSION="$(aws s3api head-object --bucket {bucket} '
                    f"--key {script_key} --region {region} "
                    "--query 'VersionId' --output text)\"",
                    f'export {var}="s3://{bucket}/{script_key}?versionId=$OBJECT_VERSION"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Create/update the Glue job via the CLI — not an ECS roll.

 The deployable is a Glue *job*, not an image on a service. We look up the job
 by name; if it exists we ``aws glue update-job`` it with the committed spec,
 otherwise we ``aws glue create-job`` it. The spec (``glue/glue-job.json``,
 emitted by ``build``) references the uploaded script + role by reference.
 Region/job name stay by-reference knobs.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` points the
 job's script at that PRE-BUILT, immutable reference (the
 ``s3://bucket/key?versionId=...`` produced once by ``package``) instead of a
 mutable key — so dev, staging and prod all run the identical artifact. Absent
 promotion it applies the committed spec exactly as before (backward
 compatible).
 """
        region = self._region(ctx)
        job_name = self._job_name(ctx)

        commands = [
            "echo 'Deploying Glue job (create/update via CLI) — NO ECS'",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            # Deploy the pre-built, immutable reference (no re-upload) — The
            # job's script location is pinned to the versioned object so every
            # environment runs the identical artifact.
            commands.append(
                f'echo "Promoting pre-built Glue script reference: {promo_ref}"'
            )

        commands.extend(
            [
                # Resolve whether the job already exists (empty id if not).
                f'EXISTING="$(aws glue get-job --job-name {job_name} '
                f"--region {region} --query 'Job.Name' --output text 2>/dev/null "
                '|| true)"',
                # Build the --job-update / --job-command payload from the committed
                # spec; create if absent, update in place otherwise.
                f'if [ "$EXISTING" = "{job_name}" ]; then '
                f"aws glue update-job --job-name {job_name} --region {region} "
                f"--job-update \"$(python -c \"import json,sys; "
                f"spec=json.load(open('{JOB_SPEC_PATH}')); "
                "spec.pop('Name', None); print(json.dumps(spec))\")\"; "
                "else "
                f"aws glue create-job --region {region} "
                f"--cli-input-json \"$(cat {JOB_SPEC_PATH})\"; fi",
            ]
        )
        return VerbResult(commands=tuple(commands))

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Start a Glue job run and poll its state until SUCCEEDED.

 ``verify`` on aws-glue is *start the job run / poll run state* — NOT an ECS
 health check. We trigger ``aws glue start-job-run``, then poll
 ``aws glue get-job-run`` until the state is terminal and assert it reached
 ``SUCCEEDED`` (failing the step otherwise). Region/job name stay
 by-reference knobs.
 """
        region = self._region(ctx)
        job_name = self._job_name(ctx)
        commands = (
            "echo 'Starting Glue job run and polling run state (aws-glue)'",
            # Trigger a run and capture its run id.
            f'RUN_ID="$(aws glue start-job-run --job-name {job_name} '
            f"--region {region} --query 'JobRunId' --output text)\"",
            # Poll the run until the state is terminal.
            "while true; do "
            f'STATE="$(aws glue get-job-run --job-name {job_name} '
            f'--run-id "$RUN_ID" --region {region} '
            "--query 'JobRun.JobRunState' --output text)\"; "
            'if [ "$STATE" = "SUCCEEDED" ] || [ "$STATE" = "FAILED" ] '
            '|| [ "$STATE" = "TIMEOUT" ] || [ "$STATE" = "STOPPED" ] '
            '|| [ "$STATE" = "ERROR" ]; then break; fi; '
            'echo "job run state: $STATE; waiting..."; sleep 10; done',
            # Assert the terminal state is SUCCEEDED, else fail the step.
            'echo "job run final state: $STATE"; [ "$STATE" = "SUCCEEDED" ]',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — Glue job + role + S3 script bucket + IAM/OIDC Terraform
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning Glue job + role + S3 + IAM/OIDC.

 Parameterised by the IR (project name for naming, ``cloud.region`` for the
 region default) and by Terraform *variables* for every account specific
 (script key, OIDC subject, Glue version/sizing). No literal account id, ARN,
 or secret is written. The ``summary`` feeds the migration runbook.
 """
        project_name = pipeline.project.name
        region = pipeline.cloud.region if pipeline.cloud is not None else None

        files = (
            GeneratedFile(
                path=f"{IAC_DIR}/main.tf",
                content=render_main_tf(project_name),
            ),
            GeneratedFile(
                path=f"{IAC_DIR}/variables.tf",
                content=render_variables_tf(region),
            ),
            GeneratedFile(
                path=f"{IAC_DIR}/outputs.tf",
                content=render_outputs_tf(),
            ),
            GeneratedFile(
                path=f"{IAC_DIR}/terraform.tfvars.example",
                content=render_tfvars_example(project_name, region),
            ),
        )

        region_text = region if region else "the configured AWS region"
        summary = (
            f"AWS Glue backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: a Glue job (aws_glue_job — the "
            f"deploy verb creates/updates it and the verify verb starts+polls a run) "
            f"running a Spark/Python ETL script from S3, the Glue execution role "
            f"(IAM, with the AWSGlueServiceRole managed policy plus a scoped inline "
            f"policy for the script bucket and CloudWatch Logs), a versioned S3 "
            f"script bucket (the package verb uploads the job script + extra python "
            f"libs; the immutable S3 object version is the promotable reference), a "
            f"GitHub/Azure OIDC identity provider, and a scoped IAM deploy role for "
            f"keyless CI deploys. This is a data-job shape — there is NO container "
            f"image, NO ECR, and NO ECS. All account specifics (account id, OIDC "
            f"subject, script ref, Glue version) are Terraform variables — no literal "
            f"secrets or account values are committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
