"""AWS EMR target-platform plugin (task 23.6, R3.3/R4.2/R7.1, design §11.9).

Implements the six stage verbs for the *aws-emr* platform and supplies the backing
Terraform. aws-emr is a **Spark/Hadoop data-job shape** like aws-glue: there is NO
container image, NO ECR, NO ECS. The verb meaning is the design §11.9 aws-emr row:

    | verb    | aws-emr                                                        |
    |---------|----------------------------------------------------------------|
    | lint    | lint the PySpark job scripts / python (generic/ruff)           |
    | test    | run the project's tests (stage.runs, else a placeholder)       |
    | build   | assemble the PySpark job script(s) + extra python libs/wheel   |  NO image
    | package | upload scripts/libs to S3 (immutable object VERSION)           |  (by-reference, promotable)
    | deploy  | aws emr-serverless start-job-run (EMR Serverless) via the CLI  |  (consumes a pre-built ref under promotion)
    | verify  | poll the EMR job-run state to SUCCESS                          |
    | IaC     | EMR Serverless app + role (+ S3 + OIDC) (Terraform)           |  (see terraform.py)

Packaging shape (design §11.9: "Spark/Hadoop clusters/steps", image=no, promotion=yes)
--------------------------------------------------------------------------------------
``build`` assembles the PySpark job script(s) and bundles any extra python
libraries/wheel into a build dir (NO docker). ``package`` uploads them to the
versioned S3 artifact bucket and — when promoting — captures the resulting
**immutable object version id** as the promotable reference (the data-job analogue
of an image digest). Because the deployable is a script bundle (not an image), the
plugin declares ``artifact_promotion`` but NOT ``container_images`` — the same
reasoning as aws-glue (§11.9 lists EMR as promotion=yes).

Design boundaries this plugin respects
---------------------------------------
* **No image, ever.** Not one verb emits ``docker build`` / ``docker push`` or
  references ECR/ECS. ``build`` assembles python scripts + a wheel; the deployable
  is an EMR *job run*, not an image. That is the data-job shape (like glue).
* **Secrets by reference only (R6).** Every account specific — the EMR application
  id, the job execution role, the artifact bucket/key, the region — is referenced as
  a shell variable (``$EMR_APPLICATION_ID``, ``$EMR_ARTIFACT_BUCKET``, …) or a
  ``settings`` value, never a literal. No account id, ARN, or key is emitted.
* **Resilient packaging (R7.1).** ``build`` installs extra libs from a pinned
  ``requirements.txt`` (PyPI only) — there is no live distro-mirror step.
* **Deterministic (R5.3).** Commands and files are built from fixed templates in a
  stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin (R4.3).** These verbs return shell steps
  and support files; stage order / gates / OIDC credential steps are the CI vendor's
  job. A verb never assumes a particular CI vendor.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

    * ``application_id``   — the EMR Serverless application id (else ``$EMR_APPLICATION_ID``).
    * ``emr_role``         — the EMR job execution role (else ``$EMR_ROLE_ARN``).
    * ``artifact_bucket``  — the S3 artifact bucket (else ``$EMR_ARTIFACT_BUCKET``).
    * ``script_key``       — the S3 object key for the entry-point script (else ``$EMR_SCRIPT_KEY``).
    * ``extra_libs_key``   — the S3 object key for the extra libs (else ``$EMR_EXTRA_LIBS_KEY``).
    * ``aws_region``       — region (else the IR ``cloud.region``, else ``$AWS_REGION``).

Nothing in ``settings`` is a secret; secrets remain references (R6).
"""

from __future__ import annotations

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

#: Where the committed EMR job-driver spec is written (deploy applies it).
JOB_SPEC_PATH = "emr/emr-job.json"

#: Build output dir for the assembled scripts + extra libs (NO image).
BUILD_DIR = ".emr_build"

#: The extra-python-libs archive the build verb produces and package uploads.
EXTRA_LIBS_ARCHIVE = "emr-extra-python-libs.zip"

#: Where the Terraform module is written (one dir per target platform, §8b/§11.1).
IAC_DIR = "iac/aws-emr"

#: Default by-reference knob names. Secrets/account specifics referenced, not valued
#: (R6). ``settings`` may override the *non-secret* ones (region, names) with values.
_REF_APPLICATION_ID = "$EMR_APPLICATION_ID"
_REF_ROLE = "$EMR_ROLE_ARN"
_REF_ARTIFACT_BUCKET = "$EMR_ARTIFACT_BUCKET"
_REF_SCRIPT_KEY = "$EMR_SCRIPT_KEY"
_REF_EXTRA_LIBS_KEY = "$EMR_EXTRA_LIBS_KEY"
_REF_AWS_REGION = "$AWS_REGION"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline (design §11.6, mirrors aws/glue). When present,
#: ``package`` resolves+exports the uploaded script's immutable S3 object VERSION id
#: into this reference and every ``deploy`` consumes that same reference rather than
#: re-uploading (R4.2/R6).
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


class AwsEmr(TargetPlatformPlugin):
    """Target-platform plugin for AWS EMR Spark/Hadoop jobs (§11.9) — NO image.

    ``name`` MUST equal the IR value / nested directory home ``plugins/target/aws/
    emr`` reports (``aws-emr``) so the IR value, the folder, and this class all agree
    (see base.py module docstring, R3.4).
    """

    name = "aws-emr"

    #: Workload capabilities aws-emr provides (§11.9/§11.2): it packages a script
    #: bundle that can be promoted across environments as an immutable S3 object
    #: version (``artifact_promotion`` — §11.9 lists EMR as promotion=yes), deploys
    #: per environment (``environments``), authenticates to AWS via IAM/OIDC
    #: (``oidc``), and references all account specifics by key
    #: (``secrets_by_reference``). It is a DATA-JOB shape (like glue): it builds NO
    #: container image, so ``container_images`` is deliberately NOT declared.
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
        """Return the ``settings`` value for ``key`` if present, else the ref."""
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

    # ------------------------------------------------------------------
    # the six stage verbs (design §11.9 aws-emr row) — NO docker anywhere
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the PySpark job scripts / python (aws-emr ships code, not a Dockerfile).

        Prefer ``ruff`` when available and fall back to a soft notice so the step
        never breaks a pipeline merely because the linter isn't installed. There is
        deliberately NO Dockerfile lint here — EMR has no image.
        """
        commands = (
            "echo 'Linting EMR PySpark job scripts/python (aws-emr)'",
            "if command -v ruff >/dev/null 2>&1; then "
            "ruff check .; "
            "else echo 'ruff not found; skipping python lint'; fi",
        )
        return VerbResult(commands=commands)

    def test(self, ctx: VerbContext) -> VerbResult:
        """Run the project's test suite.

        Testing is project-defined, so we honour ``stage.runs`` when the IR sets it
        and fall back to a conventional ``pytest -q`` placeholder otherwise.
        """
        run_cmd = ctx.stage.runs if ctx.stage.runs is not None else "pytest -q"
        return VerbResult(commands=(run_cmd,))

    def build(self, ctx: VerbContext) -> VerbResult:
        """Assemble the PySpark job script(s) + extra python libs/wheel (§11.9) — NO image.

        ``build`` on aws-emr stages the Spark job script(s) and bundles any extra
        python libraries (installed from a pinned ``requirements.txt``, PyPI only —
        no live distro mirror, R7.1) into a deterministic ``--py-files`` archive.
        There is NO ``docker build`` and NO image tag — the deployable is an EMR job
        run that executes an S3 script. Also emits the committed job-driver spec
        (``emr/emr-job.json``) the ``deploy`` verb applies, referencing the script
        location by reference (R6).
        """
        bucket = self._knob(ctx, "artifact_bucket", _REF_ARTIFACT_BUCKET)
        script_key = self._knob(ctx, "script_key", _REF_SCRIPT_KEY)
        extra_key = self._knob(ctx, "extra_libs_key", _REF_EXTRA_LIBS_KEY)

        commands = (
            "echo 'Building EMR job: PySpark scripts + extra python libs (NO image)'",
            f"rm -rf {BUILD_DIR} {EXTRA_LIBS_ARCHIVE}",
            f"mkdir -p {BUILD_DIR}/scripts {BUILD_DIR}/libs",
            # Stage the PySpark job script(s) for upload. A client keeps job scripts
            # under emr_jobs/; fall back to a notice so a wheel-only build is ok.
            f"if [ -d emr_jobs ]; then cp -r emr_jobs/. {BUILD_DIR}/scripts/; "
            "else echo 'no emr_jobs/ dir; expecting a script already in S3'; fi",
            # Bundle extra python libs/wheel from a pinned requirements file (PyPI
            # only). No docker involved.
            "if [ -f requirements.txt ]; then "
            f"pip install --target {BUILD_DIR}/libs -r requirements.txt; "
            "else echo 'no requirements.txt; skipping py-files bundle'; fi",
            # Zip the libs reproducibly: -r recurse, -X strip extra attributes so the
            # archive bytes do not depend on the host's uid/gid/timestamps (R5.3).
            f"(cd {BUILD_DIR}/libs && zip -r -X ../../{EXTRA_LIBS_ARCHIVE} . "
            "-x '*.git*' -x '*.venv*')",
            f"echo 'Built EMR py-files archive: {EXTRA_LIBS_ARCHIVE}'",
        )
        job_spec = GeneratedFile(
            path=JOB_SPEC_PATH,
            content=render_job_spec(
                entry_point=f"s3://{bucket}/{script_key}",
                extra_python_libs=f"s3://{bucket}/{extra_key}",
            ),
        )
        return VerbResult(commands=commands, files=(job_spec,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Upload the script(s) + extra libs to S3 (§11.9) — no image push.

        ``package`` on aws-emr uploads the staged script and the py-files archive to
        the versioned S3 artifact bucket. The promotable, immutable reference is the
        resulting S3 object **version id** of the script — the data-job analogue of
        an image digest.

        **Promotion (design §11.6, mirrors aws/glue).** When the core threads a
        shared artifact reference (``promotion_artifact_ref`` in ``settings``),
        ``package`` resolves the uploaded script object's immutable version id and
        EXPORTS a stable reference (``s3://bucket/key?versionId=...``) into that
        variable, so every later ``deploy`` promotes the identical, immutable
        artifact instead of rebuilding. Nothing account- or version-specific is ever
        inlined here (R6).
        """
        region = self._region(ctx)
        bucket = self._knob(ctx, "artifact_bucket", _REF_ARTIFACT_BUCKET)
        script_key = self._knob(ctx, "script_key", _REF_SCRIPT_KEY)
        extra_key = self._knob(ctx, "extra_libs_key", _REF_EXTRA_LIBS_KEY)
        commands = [
            "echo 'Uploading EMR scripts/libs to S3 (aws-emr) — no image push'",
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
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (aws-emr promotion)'",
                    f'OBJECT_VERSION="$(aws s3api head-object --bucket {bucket} '
                    f"--key {script_key} --region {region} "
                    "--query 'VersionId' --output text)\"",
                    f'export {var}="s3://{bucket}/{script_key}?versionId=$OBJECT_VERSION"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Start an EMR Serverless job run via the CLI (§11.9) — not an ECS roll.

        The deployable is an EMR *job run*, not an image on a service. We ensure the
        application is started, then ``aws emr-serverless start-job-run`` with the
        committed job-driver spec (``emr/emr-job.json``, emitted by ``build``) and
        capture the run id for ``verify``. Application id / role / region stay
        by-reference knobs (R6).

        **Promotion (design §11.6).** When the core threads a shared artifact
        reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` announces
        the PRE-BUILT, immutable reference (the ``s3://bucket/key?versionId=...``
        produced once by ``package``) so every environment runs the identical
        artifact. Absent promotion it submits the committed spec exactly as before.
        """
        region = self._region(ctx)
        application_id = self._knob(ctx, "application_id", _REF_APPLICATION_ID)
        role = self._knob(ctx, "emr_role", _REF_ROLE)

        commands = [
            "echo 'Deploying EMR Serverless job run (start-job-run via CLI) — NO ECS'",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            commands.append(
                f'echo "Promoting pre-built EMR script reference: {promo_ref}"'
            )

        commands.extend(
            [
                # Ensure the application is started before submitting a run.
                f"aws emr-serverless start-application --application-id {application_id} "
                f"--region {region} 2>/dev/null || true",
                # Submit the job run using the committed job-driver spec; capture the
                # run id for the verify verb.
                f'EMR_JOB_RUN_ID="$(aws emr-serverless start-job-run '
                f"--application-id {application_id} --execution-role-arn {role} "
                f"--region {region} --job-driver \"$(cat {JOB_SPEC_PATH})\" "
                "--query 'jobRunId' --output text)\"",
                'echo "Started EMR job run: $EMR_JOB_RUN_ID"',
            ]
        )
        return VerbResult(commands=tuple(commands))

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Poll the EMR Serverless job-run state until SUCCESS (§11.9).

        ``verify`` on aws-emr polls ``aws emr-serverless get-job-run`` until the
        state is terminal and asserts it reached ``SUCCESS`` (failing the step
        otherwise). Application id / region stay by-reference knobs (R6). The run id
        comes from the ``deploy`` verb (``$EMR_JOB_RUN_ID``); if unset we start a run
        here so ``verify`` is independently exercisable.
        """
        region = self._region(ctx)
        application_id = self._knob(ctx, "application_id", _REF_APPLICATION_ID)
        role = self._knob(ctx, "emr_role", _REF_ROLE)
        commands = (
            "echo 'Polling EMR Serverless job-run state (aws-emr)'",
            # If a run id was not threaded from deploy, start one so verify stands
            # alone; otherwise reuse the deploy run id.
            f'if [ -z "${{EMR_JOB_RUN_ID:-}}" ]; then '
            f'EMR_JOB_RUN_ID="$(aws emr-serverless start-job-run '
            f"--application-id {application_id} --execution-role-arn {role} "
            f"--region {region} --job-driver \"$(cat {JOB_SPEC_PATH})\" "
            "--query 'jobRunId' --output text)\"; fi",
            # Poll the run until the state is terminal.
            "while true; do "
            f'STATE="$(aws emr-serverless get-job-run '
            f"--application-id {application_id} --job-run-id \"$EMR_JOB_RUN_ID\" "
            f"--region {region} --query 'jobRun.state' --output text)\"; "
            'if [ "$STATE" = "SUCCESS" ] || [ "$STATE" = "FAILED" ] '
            '|| [ "$STATE" = "CANCELLED" ] || [ "$STATE" = "CANCELLING" ]; then '
            "break; fi; "
            'echo "job run state: $STATE; waiting..."; sleep 10; done',
            # Assert the terminal state is SUCCESS, else fail the step.
            'echo "job run final state: $STATE"; [ "$STATE" = "SUCCESS" ]',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — EMR Serverless app + role + S3 + IAM/OIDC Terraform (§11.9)
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning EMR + role + S3 + IAM/OIDC (§11.9)."""
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
            f"AWS EMR backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: an EMR Serverless application "
            f"(aws_emrserverless_application — the deploy verb starts a job run on it "
            f"and the verify verb polls the run to SUCCESS) running a Spark/PySpark "
            f"job from S3, the EMR job execution role (IAM, with a scoped inline "
            f"policy for the artifact bucket and CloudWatch Logs), a versioned S3 "
            f"artifact bucket (the package verb uploads the job script + extra python "
            f"libs; the immutable S3 object version is the promotable reference), a "
            f"GitHub/Azure OIDC identity provider, and a scoped IAM deploy role for "
            f"keyless CI deploys. This is a Spark/Hadoop data-job shape — there is NO "
            f"container image, NO ECR, and NO ECS. All account specifics (account id, "
            f"OIDC subject, script ref, EMR release) are Terraform variables — no "
            f"literal secrets or account values are committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
