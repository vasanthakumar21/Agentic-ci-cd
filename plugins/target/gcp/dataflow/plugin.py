"""GCP Dataflow target-platform plugin.

Implements the six stage verbs for the *gcp-dataflow* platform and supplies the
backing Terraform. gcp-dataflow is a **data-job shape** like aws-glue/databricks:
there is NO required container image, NO Artifact Registry, NO Cloud Run. The verb
meaning is the gcp-dataflow row:

 | verb | gcp-dataflow |
 |---------|----------------------------------------------------------------|
 | lint | lint the Beam pipeline scripts / python (generic/ruff) |
 | test | run the project's tests (stage.runs, else a placeholder) |
 | build | assemble the Beam pipeline + stage a (classic) template spec | NO image
 | package | stage the template + artifacts to GCS (immutable GENERATION) | (by-reference, promotable)
 | deploy | gcloud dataflow jobs run (from the staged template) | (consumes a pre-built ref under promotion)
 | verify | poll the Dataflow job state to JOB_STATE_DONE |
 | IaC | GCS bucket + Dataflow worker SA + deploy SA + WIF (Terraform) | (see terraform.py)

Packaging shape
-------------------------------------------------------------------------------
``build`` assembles the Beam pipeline and stages a (classic) template spec into a
build dir (NO docker). ``package`` uploads the template + artifacts to the versioned
GCS bucket and — when promoting — captures the resulting **immutable object
generation number** as the promotable reference (the data-job analogue of an image
digest). We prefer the staged-template path to stay no-image; a flex-template image,
when used, is only a by-reference knob the client supplies. Because the deployable is
a staged template (not an image), the plugin declares ``artifact_promotion`` but NOT
``container_images`` — the same reasoning as aws-glue.

Design boundaries this plugin respects
---------------------------------------
* **No image, ever.** Not one verb emits ``docker build`` / ``docker push`` or
 references Artifact Registry / Cloud Run. ``build`` stages a template; the
 deployable is a Dataflow *job* launched from that template, not an image. That is
 the data-job shape (like aws-glue).
* **Keyless CI via WIF.** Authentication is keyless via Workload Identity
 Federation: the CI OIDC token federates into a deploy service account (no JSON
 key). The credential step is the CI vendor's job — a verb never emits a key.
* **Secrets by reference only.** Every project specific — the GCP project id,
 the region, the bucket, the template path, the job name — is referenced as a
 shell variable (``$GCP_PROJECT``, ``$GCP_REGION``, ``$DATAFLOW_BUCKET``, …) or a
 ``settings`` value, never a literal.
* **Resilient packaging.** ``build`` installs extra libs from a pinned
 ``requirements.txt`` (PyPI only) — no live distro-mirror step that could make the
 build go red for reasons unrelated to the change.
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / OIDC-federation / gcloud-auth steps are
 the CI vendor's job. A verb never assumes a particular CI vendor.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``gcp_project`` — the GCP project id (else the ``$GCP_PROJECT`` ref).
 * ``gcp_region`` — region (else the IR ``cloud.region``, else ``$GCP_REGION``).
 * ``dataflow_bucket``— the GCS bucket (else ``$DATAFLOW_BUCKET``).
 * ``template_key`` — the GCS object key for the staged template (else ``$DATAFLOW_TEMPLATE_KEY``).
 * ``job_name`` — the Dataflow job name (else ``<project>-job`` via the slug).

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

from .jobspec import render_template_metadata
from .terraform import (
    render_main_tf,
    render_outputs_tf,
    render_tfvars_example,
    render_variables_tf,
)

#: Where the committed Dataflow template metadata is written (deploy launches it).
TEMPLATE_SPEC_PATH = "dataflow/dataflow-template.json"

#: Build output dir for the assembled pipeline + staged template (NO image).
BUILD_DIR = ".dataflow_build"

#: The staged template artifact the build verb produces and package uploads.
TEMPLATE_ARTIFACT = "dataflow-template.json"

#: Where the Terraform module is written (one dir per target platform, /).
IAC_DIR = "iac/gcp-dataflow"

#: Default by-reference knob names. Project specifics referenced, not valued.
#: ``settings`` may override the *non-secret* ones (region, names) with values.
_REF_GCP_PROJECT = "$GCP_PROJECT"
_REF_GCP_REGION = "$GCP_REGION"
_REF_DATAFLOW_BUCKET = "$DATAFLOW_BUCKET"
_REF_TEMPLATE_KEY = "$DATAFLOW_TEMPLATE_KEY"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline.
#: When present, ``package`` resolves+exports the staged template's immutable GCS
#: object GENERATION number into this reference and every ``deploy`` consumes that
#: same reference rather than re-staging.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


def _job_slug(raw: str) -> str:
    """Dash-based job-name prefix — MUST match terraform ``_sanitize_name``.

 The Dataflow job name produced by the CLI ``deploy`` verb and by the Terraform
 module must share a naming convention so the two paths agree. Both normalise the
 IR project name to ``[a-z0-9-]`` (dashes).
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


class GcpDataflow(TargetPlatformPlugin):
    """Target-platform plugin for GCP Dataflow Apache Beam pipelines — NO image.

 ``name`` MUST equal the IR value / nested directory home
 ``plugins/target/gcp/dataflow`` resolves to (``gcp-dataflow`` via the ``gcp-``
 prefix rule, splitting on the FIRST ``-``) so the IR value, the folder, and this
 class all agree (see base.py module docstring,).
 """

    name = "gcp-dataflow"

    #: Workload capabilities gcp-dataflow provides: it stages a template
    #: that can be promoted across environments as an immutable GCS object generation
    #: (``artifact_promotion`` — lists Dataflow as promotion=yes), deploys per
    #: environment (``environments``), authenticates to GCP keylessly via Workload
    #: Identity Federation / OIDC (``oidc``), and references all project specifics by
    #: key (``secrets_by_reference``). It is a DATA-JOB shape (like aws-glue): it
    #: builds NO container image (staged-template path), so ``container_images`` is
    #: deliberately NOT declared.
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
 (region, bucket/template/job names) may be inlined from ``settings``; anything
 not supplied falls back to a shell *reference* so no project specific is ever
 guessed or hard-coded.
 """
        value = ctx.settings.get(key)
        return value if value else ref

    def _region(self, ctx: VerbContext) -> str:
        """Resolve the region: explicit setting > IR cloud.region > ``$GCP_REGION``."""
        if ctx.settings.get("gcp_region"):
            return ctx.settings["gcp_region"]
        cloud = ctx.pipeline.cloud
        if cloud is not None and cloud.region:
            return cloud.region
        return _REF_GCP_REGION

    def _job_name(self, ctx: VerbContext) -> str:
        """Job name: explicit setting > ``<project>-job`` (non-secret, derived)."""
        if ctx.settings.get("job_name"):
            return ctx.settings["job_name"]
        return f"{_job_slug(ctx.pipeline.project.name)}-job"

    # ------------------------------------------------------------------
    # the six stage verbs — NO docker anywhere
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the Beam pipeline scripts / python (gcp-dataflow ships code, no image).

 Prefer ``ruff`` when available and fall back to a soft notice so the step
 never breaks a pipeline merely because the linter isn't installed. There is
 deliberately NO Dockerfile lint here — the staged-template path has no image.
 """
        commands = (
            "echo 'Linting Dataflow Beam pipeline scripts/python (gcp-dataflow)'",
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
        """Assemble the Beam pipeline + stage a classic template spec — NO image.

 ``build`` on gcp-dataflow stages the Beam pipeline and builds a (classic)
 Dataflow template spec into a build dir, installing any extra python
 libraries from a pinned ``requirements.txt`` (PyPI only — no live distro
 mirror,). There is NO ``docker build`` and NO image tag — we prefer the
 staged-template path to stay no-image (a flex-template image, if ever used, is
 a by-reference knob). Also emits the committed template metadata
 (``dataflow/dataflow-template.json``) the ``deploy`` verb launches from,
 referencing the template path + locations by reference.
 """
        bucket = self._knob(ctx, "dataflow_bucket", _REF_DATAFLOW_BUCKET)
        template_key = self._knob(ctx, "template_key", _REF_TEMPLATE_KEY)
        job_name = self._job_name(ctx)

        commands = (
            "echo 'Building Dataflow job: Beam pipeline + staged (classic) template (NO image)'",
            f"rm -rf {BUILD_DIR}",
            f"mkdir -p {BUILD_DIR}",
            # Install the pipeline's extra python libs from a pinned requirements file
            # (PyPI only). No docker involved.
            "if [ -f requirements.txt ]; then "
            f"pip install --target {BUILD_DIR}/libs -r requirements.txt; "
            "else echo 'no requirements.txt; skipping extra-python-files bundle'; fi",
            # Stage the classic template. A client keeps the Beam pipeline under
            # dataflow_pipeline/; fall back to a notice so a template-only build is ok.
            f"if [ -f dataflow_pipeline/pipeline.py ]; then "
            f"python dataflow_pipeline/pipeline.py "
            f"--runner DataflowRunner "
            f"--template_location {BUILD_DIR}/{TEMPLATE_ARTIFACT}; "
            "else echo 'no dataflow_pipeline/pipeline.py; expecting a template already in GCS'; fi",
            f"echo 'Staged Dataflow template artifact: {TEMPLATE_ARTIFACT}'",
        )
        template_spec = GeneratedFile(
            path=TEMPLATE_SPEC_PATH,
            content=render_template_metadata(
                job_name=job_name,
                template_path=f"gs://{bucket}/{template_key}",
                temp_location=f"gs://{bucket}/temp",
                staging_location=f"gs://{bucket}/staging",
            ),
        )
        return VerbResult(commands=commands, files=(template_spec,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Stage the template + artifacts to GCS — no image push.

 ``package`` on gcp-dataflow uploads the staged template to the versioned GCS
 bucket with ``gcloud storage cp``. The promotable, immutable reference is the
 resulting GCS object **generation number** of the template — the data-job
 analogue of an image digest. Authentication is the CI vendor's WIF/OIDC step
 (not emitted here); the bucket/key are by-reference knobs.

 **Promotion.** When the core
 threads a shared artifact reference (``promotion_artifact_ref`` in
 ``settings``), ``package`` resolves the staged template object's immutable
 generation number and EXPORTS a stable reference
 (``gs://bucket/key#generation``) into that variable, so every later
 ``deploy`` promotes the identical, immutable template instead of re-staging.
 Nothing project- or generation-specific is ever inlined here.
 """
        project = self._knob(ctx, "gcp_project", _REF_GCP_PROJECT)
        bucket = self._knob(ctx, "dataflow_bucket", _REF_DATAFLOW_BUCKET)
        template_key = self._knob(ctx, "template_key", _REF_TEMPLATE_KEY)
        commands = [
            "echo 'Staging Dataflow template to GCS (gcp-dataflow) — no image push'",
            f"if [ -f {BUILD_DIR}/{TEMPLATE_ARTIFACT} ]; then "
            f"gcloud storage cp {BUILD_DIR}/{TEMPLATE_ARTIFACT} "
            f"gs://{bucket}/{template_key} --project {project}; "
            "else echo 'no local template staged; assuming template already in GCS'; fi",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            # Resolve the immutable GCS object generation for the staged template and
            # export it as the shared promotable reference. Every deploy consumes
            # this exact reference — build once, promote the same template.
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (gcp-dataflow promotion)'",
                    f'OBJECT_GENERATION="$(gcloud storage objects describe '
                    f"gs://{bucket}/{template_key} --project {project} "
                    "--format='value(generation)')\"",
                    f'export {var}="gs://{bucket}/{template_key}#$OBJECT_GENERATION"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Launch a Dataflow job from the staged template via the CLI — not a roll.

 The deployable is a Dataflow *job* launched from a staged template, not an
 image on a service. We run ``gcloud dataflow jobs run`` from the GCS-staged
 template. Region/project/bucket/job name stay by-reference knobs.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` launches the
 job from that PRE-BUILT, immutable reference (the
 ``gs://bucket/key#generation`` produced once by ``package``) instead of a
 mutable key — so dev, staging and prod all run the identical template. Absent
 promotion it launches from the committed spec's template path (backward
 compatible).
 """
        region = self._region(ctx)
        project = self._knob(ctx, "gcp_project", _REF_GCP_PROJECT)
        bucket = self._knob(ctx, "dataflow_bucket", _REF_DATAFLOW_BUCKET)
        template_key = self._knob(ctx, "template_key", _REF_TEMPLATE_KEY)
        job_name = self._job_name(ctx)

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            # Launch from the pre-built, immutable reference (no re-stage) —
            template_uri = promo_ref
        else:
            template_uri = f"gs://{bucket}/{template_key}"

        commands = [
            "echo 'Launching Dataflow job from staged template (gcloud dataflow jobs run) — NO image'",
        ]
        if promo_ref:
            commands.append(
                f'echo "Promoting pre-built Dataflow template reference: {promo_ref}"'
            )
        commands.append(
            f"gcloud dataflow jobs run {job_name} "
            f"--gcs-location {template_uri} --project {project} --region {region} "
            f"--staging-location gs://{bucket}/staging"
        )
        return VerbResult(commands=tuple(commands))

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Poll the Dataflow job state until terminal and assert JOB_STATE_DONE.

 ``verify`` on gcp-dataflow is *poll the job state* — NOT a Cloud Run health
 check. We resolve the most recent job id for the job name, poll
 ``gcloud dataflow jobs describe`` until the state is terminal, and assert it
 reached ``JOB_STATE_DONE`` (failing the step on
 ``JOB_STATE_FAILED``/``JOB_STATE_CANCELLED``). Region/project/job name stay
 by-reference knobs.
 """
        region = self._region(ctx)
        project = self._knob(ctx, "gcp_project", _REF_GCP_PROJECT)
        job_name = self._job_name(ctx)
        commands = (
            "echo 'Polling Dataflow job state (gcp-dataflow)'",
            # Resolve the most recent job id for this job name.
            f'JOB_ID="$(gcloud dataflow jobs list --project {project} '
            f"--region {region} --filter=\"name={job_name}\" "
            "--sort-by=~createTime --limit=1 --format='value(id)')\"",
            # Poll the job until the state is terminal.
            "while true; do "
            f'STATE="$(gcloud dataflow jobs describe "$JOB_ID" '
            f"--project {project} --region {region} "
            "--format='value(currentState)')\"; "
            'if [ "$STATE" = "JOB_STATE_DONE" ] || [ "$STATE" = "JOB_STATE_FAILED" ] '
            '|| [ "$STATE" = "JOB_STATE_CANCELLED" ]; then break; fi; '
            'echo "job state: $STATE; waiting..."; sleep 10; done',
            # Assert the terminal state is JOB_STATE_DONE, else fail the step.
            'echo "job final state: $STATE"; [ "$STATE" = "JOB_STATE_DONE" ]',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — GCS bucket + Dataflow worker SA + deploy SA + WIF Terraform
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning Dataflow + GCS + SA + WIF.

 Parameterised by the IR (project name for naming, ``cloud.region`` for the
 region default) and by Terraform *variables* for every project specific
 (project id, CI repo, template key). No literal GCP project id, service
 account key, or secret is written — CI authenticates keylessly by
 federating its OIDC token into the deploy service account via Workload
 Identity Federation. The ``summary`` feeds the migration runbook.
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

        region_text = region if region else "the configured GCP region"
        summary = (
            f"GCP Dataflow backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: a versioned GCS bucket (the package "
            f"verb stages the Dataflow template + staging/temp locations; the "
            f"immutable GCS object generation is the promotable reference), a "
            f"Dataflow worker service account the jobs run as, a deploy service "
            f"account scoped to stage templates and launch Dataflow jobs (the deploy "
            f"verb runs the job from the staged template and the verify verb polls "
            f"its state to JOB_STATE_DONE), and a Workload Identity Federation (WIF) "
            f"pool + OIDC provider bound to the client CI repository so the pipeline "
            f"authenticates keylessly (no service account JSON key — the GCP "
            f"keyless-OIDC story). This is a data-job shape — the job launches from a "
            f"staged TEMPLATE, so there is NO container image, NO Artifact Registry, "
            f"and NO Cloud Run. All project specifics (project id, CI repo, OIDC "
            f"issuer, template key) are Terraform variables — no literal secrets or "
            f"project values are committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
