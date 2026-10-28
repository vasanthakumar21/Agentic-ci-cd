"""GCP Dataproc target-platform plugin.

Implements the six stage verbs for the *gcp-dataproc* platform and supplies the
backing Terraform. gcp-dataproc is a **data-job shape** like aws-glue/databricks:
there is NO container image, NO Artifact Registry, NO Cloud Run. The verb meaning
is the gcp-dataproc row:

 | verb | gcp-dataproc |
 |---------|----------------------------------------------------------------|
 | lint | lint the PySpark job scripts / python (generic/ruff) |
 | test | run the project's tests (stage.runs, else a placeholder) |
 | build | assemble the PySpark job script(s) + extra python libs/wheel | NO image
 | package | upload scripts/libs to a GCS bucket (immutable GENERATION) | (by-reference, promotable)
 | deploy | gcloud dataproc batches submit pyspark (Serverless batch) | (consumes a pre-built ref under promotion)
 | verify | poll the Dataproc batch state to SUCCEEDED |
 | IaC | Dataproc batch + GCS staging + SA + WIF (Terraform) | (see terraform.py)

Packaging shape
------------------------------------------------------------------------------
``build`` assembles the PySpark job script(s) and any extra python libs/wheel into
a build dir (NO docker). ``package`` uploads them to the versioned GCS staging
bucket and — when promoting — captures the resulting **immutable object generation
number** as the promotable reference (the data-job analogue of an image digest).
Because the deployable is a script bundle (not an image), the plugin declares
``artifact_promotion`` but NOT ``container_images`` — the same reasoning as aws-glue.

Design boundaries this plugin respects
---------------------------------------
* **No image, ever.** Not one verb emits ``docker build`` / ``docker push`` or
 references Artifact Registry / Cloud Run. ``build`` assembles python scripts + a
 wheel; the deployable is a Dataproc *batch*, not an image. That is the data-job
 shape (like aws-glue).
* **Keyless CI via WIF.** Authentication is keyless via Workload Identity
 Federation: the CI OIDC token federates into a deploy service account (no JSON
 key). The credential step is the CI vendor's job — a verb never emits a key.
* **Secrets by reference only.** Every project specific — the GCP project id,
 the region, the staging bucket, the batch id — is referenced as a shell variable
 (``$GCP_PROJECT``, ``$GCP_REGION``, ``$DATAPROC_STAGING_BUCKET``, …) or a
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
 * ``staging_bucket`` — the GCS staging bucket (else ``$DATAPROC_STAGING_BUCKET``).
 * ``main_script_key`` — the GCS object key for the main script (else ``$DATAPROC_MAIN_SCRIPT_KEY``).
 * ``extra_libs_key`` — the GCS object key for the extra libs (else ``$DATAPROC_EXTRA_LIBS_KEY``).
 * ``batch_id`` — the Dataproc batch id (else ``<project>-batch`` via the slug).

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

from .jobspec import render_batch_spec
from .terraform import (
    render_main_tf,
    render_outputs_tf,
    render_tfvars_example,
    render_variables_tf,
)

#: Where the committed Dataproc batch spec is written (deploy submits it).
BATCH_SPEC_PATH = "dataproc/dataproc-batch.json"

#: Build output dir for the assembled scripts + extra libs (NO image).
BUILD_DIR = ".dataproc_build"

#: The extra-python-libs archive the build verb produces and package uploads.
EXTRA_LIBS_ARCHIVE = "dataproc-extra-python-libs.zip"

#: Where the Terraform module is written (one dir per target platform, /).
IAC_DIR = "iac/gcp-dataproc"

#: Default by-reference knob names. Project specifics referenced, not valued.
#: ``settings`` may override the *non-secret* ones (region, names) with values.
_REF_GCP_PROJECT = "$GCP_PROJECT"
_REF_GCP_REGION = "$GCP_REGION"
_REF_STAGING_BUCKET = "$DATAPROC_STAGING_BUCKET"
_REF_MAIN_SCRIPT_KEY = "$DATAPROC_MAIN_SCRIPT_KEY"
_REF_EXTRA_LIBS_KEY = "$DATAPROC_EXTRA_LIBS_KEY"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline.
#: When present, ``package`` resolves+exports the uploaded script's immutable GCS
#: object GENERATION number into this reference and every ``deploy`` consumes that
#: same reference rather than re-uploading.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


def _batch_slug(raw: str) -> str:
    """Dash-based batch-id prefix — MUST match terraform ``_sanitize_name``.

 The Dataproc batch id produced by the CLI ``deploy`` verb and by the Terraform
 ``google_dataproc_batch`` must share a naming convention so the two paths agree.
 Both normalise the IR project name to ``[a-z0-9-]`` (dashes).
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


class GcpDataproc(TargetPlatformPlugin):
    """Target-platform plugin for GCP Dataproc Spark/Hadoop jobs — NO image.

 ``name`` MUST equal the IR value / nested directory home
 ``plugins/target/gcp/dataproc`` resolves to (``gcp-dataproc`` via the ``gcp-``
 prefix rule, splitting on the FIRST ``-``) so the IR value, the folder, and this
 class all agree (see base.py module docstring,).
 """

    name = "gcp-dataproc"

    #: Workload capabilities gcp-dataproc provides: it packages a
    #: script bundle that can be promoted across environments as an immutable GCS
    #: object generation (``artifact_promotion`` — lists Dataproc as
    #: promotion=yes), deploys per environment (``environments``), authenticates to
    #: GCP keylessly via Workload Identity Federation / OIDC (``oidc``), and
    #: references all project specifics by key (``secrets_by_reference``). It is a
    #: DATA-JOB shape (like aws-glue): it builds NO container image, so
    #: ``container_images`` is deliberately NOT declared.
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
 (region, bucket/key/batch names) may be inlined from ``settings``; anything
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

    def _batch_id(self, ctx: VerbContext) -> str:
        """Batch id: explicit setting > ``<project>-batch`` (non-secret, derived)."""
        if ctx.settings.get("batch_id"):
            return ctx.settings["batch_id"]
        return f"{_batch_slug(ctx.pipeline.project.name)}-batch"

    # ------------------------------------------------------------------
    # the six stage verbs — NO docker anywhere
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the PySpark job scripts / python (gcp-dataproc ships code, no image).

 Prefer ``ruff`` when available and fall back to a soft notice so the step
 never breaks a pipeline merely because the linter isn't installed. There is
 deliberately NO Dockerfile lint here — dataproc has no image.
 """
        commands = (
            "echo 'Linting Dataproc PySpark job scripts/python (gcp-dataproc)'",
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
        """Assemble the PySpark job script(s) + extra python libs/wheel — NO image.

 ``build`` on gcp-dataproc stages the PySpark job script(s) and bundles any
 extra python libraries (installed from a pinned ``requirements.txt``, PyPI
 only — no live distro mirror,) into a deterministic ``pythonFileUris``
 archive. There is NO ``docker build`` and NO image tag — the deployable is a
 Dataproc batch that runs a GCS-staged script. Also emits the committed batch
 spec (``dataproc/dataproc-batch.json``) the ``deploy`` verb submits,
 referencing the script location + staging bucket by reference.
 """
        bucket = self._knob(ctx, "staging_bucket", _REF_STAGING_BUCKET)
        main_key = self._knob(ctx, "main_script_key", _REF_MAIN_SCRIPT_KEY)
        extra_key = self._knob(ctx, "extra_libs_key", _REF_EXTRA_LIBS_KEY)

        commands = (
            "echo 'Building Dataproc batch: PySpark scripts + extra python libs (NO image)'",
            f"rm -rf {BUILD_DIR} {EXTRA_LIBS_ARCHIVE}",
            f"mkdir -p {BUILD_DIR}/scripts {BUILD_DIR}/libs",
            # Stage the PySpark job script(s) for upload. A client keeps job scripts
            # under dataproc_jobs/; fall back to a notice so a wheel-only build is ok.
            f"if [ -d dataproc_jobs ]; then cp -r dataproc_jobs/. {BUILD_DIR}/scripts/; "
            "else echo 'no dataproc_jobs/ dir; expecting a script already in GCS'; fi",
            # Bundle extra python libs/wheel from a pinned requirements file (PyPI
            # only). No docker involved.
            "if [ -f requirements.txt ]; then "
            f"pip install --target {BUILD_DIR}/libs -r requirements.txt; "
            "else echo 'no requirements.txt; skipping extra-python-files bundle'; fi",
            # Zip the libs reproducibly: -r recurse, -X strip extra attributes so the
            # archive bytes do not depend on the host's uid/gid/timestamps.
            f"(cd {BUILD_DIR}/libs && zip -r -X ../../{EXTRA_LIBS_ARCHIVE} . "
            "-x '*.git*' -x '*.venv*')",
            f"echo 'Built Dataproc python-file-uris archive: {EXTRA_LIBS_ARCHIVE}'",
        )
        batch_spec = GeneratedFile(
            path=BATCH_SPEC_PATH,
            content=render_batch_spec(
                main_python_file_uri=f"gs://{bucket}/{main_key}",
                extra_python_file_uri=f"gs://{bucket}/{extra_key}",
                staging_bucket=f"gs://{bucket}",
            ),
        )
        return VerbResult(commands=commands, files=(batch_spec,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Upload the script(s) + extra libs to GCS — no image push.

 ``package`` on gcp-dataproc uploads the staged script and the extra-libs
 archive to the versioned GCS staging bucket with ``gcloud storage cp``. The
 promotable, immutable reference is the resulting GCS object **generation
 number** of the main script — the data-job analogue of an image digest.
 Authentication is the CI vendor's WIF/OIDC step (not emitted here); the
 bucket/keys are by-reference knobs.

 **Promotion.** When the core
 threads a shared artifact reference (``promotion_artifact_ref`` in
 ``settings``), ``package`` resolves the uploaded script object's immutable
 generation number and EXPORTS a stable reference
 (``gs://bucket/key#generation``) into that variable, so every later
 ``deploy`` promotes the identical, immutable artifact instead of rebuilding.
 Nothing project- or generation-specific is ever inlined here.
 """
        project = self._knob(ctx, "gcp_project", _REF_GCP_PROJECT)
        bucket = self._knob(ctx, "staging_bucket", _REF_STAGING_BUCKET)
        main_key = self._knob(ctx, "main_script_key", _REF_MAIN_SCRIPT_KEY)
        extra_key = self._knob(ctx, "extra_libs_key", _REF_EXTRA_LIBS_KEY)
        commands = [
            "echo 'Uploading Dataproc scripts/libs to GCS (gcp-dataproc) — no image push'",
            f"gcloud storage cp {EXTRA_LIBS_ARCHIVE} gs://{bucket}/{extra_key} "
            f"--project {project}",
            # Upload the first staged job script under the main script key.
            f'SCRIPT="$(ls {BUILD_DIR}/scripts/*.py 2>/dev/null | head -n1)"; '
            f'if [ -n "$SCRIPT" ]; then '
            f'gcloud storage cp "$SCRIPT" gs://{bucket}/{main_key} --project {project}; '
            "else echo 'no local script staged; assuming script already in GCS'; fi",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            # Resolve the immutable GCS object generation for the uploaded script and
            # export it as the shared promotable reference. Every deploy consumes
            # this exact reference — build once, promote the same artifact.
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (gcp-dataproc promotion)'",
                    f'OBJECT_GENERATION="$(gcloud storage objects describe '
                    f"gs://{bucket}/{main_key} --project {project} "
                    "--format='value(generation)')\"",
                    f'export {var}="gs://{bucket}/{main_key}#$OBJECT_GENERATION"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Submit a Dataproc Serverless PySpark batch via the CLI — not a roll.

 The deployable is a Dataproc *batch*, not an image on a service. We submit a
 PySpark batch with ``gcloud dataproc batches submit pyspark`` pointed at the
 GCS-staged main script, bringing in the extra libs via ``--py-files``.
 Region/project/bucket stay by-reference knobs.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` submits the
 batch against that PRE-BUILT, immutable reference (the
 ``gs://bucket/key#generation`` produced once by ``package``) instead of a
 mutable key — so dev, staging and prod all run the identical artifact. Absent
 promotion it submits from the committed spec's script location (backward
 compatible).
 """
        region = self._region(ctx)
        project = self._knob(ctx, "gcp_project", _REF_GCP_PROJECT)
        bucket = self._knob(ctx, "staging_bucket", _REF_STAGING_BUCKET)
        main_key = self._knob(ctx, "main_script_key", _REF_MAIN_SCRIPT_KEY)
        extra_key = self._knob(ctx, "extra_libs_key", _REF_EXTRA_LIBS_KEY)
        batch_id = self._batch_id(ctx)

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            # Submit the pre-built, immutable reference (no re-upload) —
            main_uri = promo_ref
        else:
            main_uri = f"gs://{bucket}/{main_key}"

        commands = [
            "echo 'Submitting Dataproc PySpark batch (gcloud dataproc batches submit) — NO image'",
        ]
        if promo_ref:
            commands.append(
                f'echo "Promoting pre-built Dataproc script reference: {promo_ref}"'
            )
        commands.append(
            f"gcloud dataproc batches submit pyspark {main_uri} "
            f"--batch {batch_id} --project {project} --region {region} "
            f"--py-files gs://{bucket}/{extra_key}"
        )
        return VerbResult(commands=tuple(commands))

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Poll the Dataproc batch state until terminal and assert SUCCEEDED.

 ``verify`` on gcp-dataproc is *poll the batch state* — NOT a Cloud Run health
 check. We poll ``gcloud dataproc batches describe`` until the state is
 terminal and assert it reached ``SUCCEEDED`` (failing the step on
 ``FAILED``/``CANCELLED``). Region/project/batch id stay by-reference knobs.
 """
        region = self._region(ctx)
        project = self._knob(ctx, "gcp_project", _REF_GCP_PROJECT)
        batch_id = self._batch_id(ctx)
        commands = (
            "echo 'Polling Dataproc batch state (gcp-dataproc)'",
            # Poll the batch until the state is terminal.
            "while true; do "
            f"STATE=\"$(gcloud dataproc batches describe {batch_id} "
            f"--project {project} --region {region} "
            "--format='value(state)')\"; "
            'if [ "$STATE" = "SUCCEEDED" ] || [ "$STATE" = "FAILED" ] '
            '|| [ "$STATE" = "CANCELLED" ]; then break; fi; '
            'echo "batch state: $STATE; waiting..."; sleep 10; done',
            # Assert the terminal state is SUCCEEDED, else fail the step.
            'echo "batch final state: $STATE"; [ "$STATE" = "SUCCEEDED" ]',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — Dataproc batch + GCS staging + SA + WIF Terraform
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning Dataproc + GCS + SA + WIF.

 Parameterised by the IR (project name for naming, ``cloud.region`` for the
 region default) and by Terraform *variables* for every project specific
 (project id, CI repo, object keys). No literal GCP project id, service account
 key, or secret is written — CI authenticates keylessly by federating its
 OIDC token into the deploy service account via Workload Identity Federation.
 The ``summary`` feeds the migration runbook.
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
            f"GCP Dataproc backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: a Dataproc Serverless PySpark batch "
            f"(google_dataproc_batch — the deploy verb submits it and the verify verb "
            f"polls its state to SUCCEEDED) running a Spark/Python job from GCS, a "
            f"Dataproc runtime service account, a versioned GCS staging bucket (the "
            f"package verb uploads the job script + extra python libs; the immutable "
            f"GCS object generation is the promotable reference), a deploy service "
            f"account scoped to stage artifacts and submit batches, and a Workload "
            f"Identity Federation (WIF) pool + OIDC provider bound to the client CI "
            f"repository so the pipeline authenticates keylessly (no service account "
            f"JSON key — the GCP keyless-OIDC story). This is a data-job shape — there "
            f"is NO container image, NO Artifact Registry, and NO Cloud Run. All "
            f"project specifics (project id, CI repo, OIDC issuer, object keys) are "
            f"Terraform variables — no literal secrets or project values are "
            f"committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
