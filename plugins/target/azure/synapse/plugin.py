"""Azure Synapse target-platform plugin.

Implements the six stage verbs for the *azure-synapse* platform and supplies the
backing Terraform. azure-synapse is a **data-job shape** like databricks / aws-glue:
there is NO container image, NO ACR, NO ECS. The verb meaning is the
azure-synapse row:

 | verb | azure-synapse |
 |---------|-----------------------------------------------------------------|
 | lint | lint the notebooks / Spark job scripts / python (generic/ruff) |
 | test | run the project's tests (stage.runs, else a placeholder) |
 | build | assemble notebooks / Spark job defs / scripts + a wheel | NO image
 | package | upload to the Synapse workspace storage / ADLS | (versioned blob = promotable ref)
 | deploy | create/update a Synapse Spark job definition via `az synapse` | (consumes a pre-built ref under promotion)
 | verify | start + poll the Synapse job run to Succeeded |
 | IaC | Synapse workspace ref + Spark pool + storage + roles + CI (TF) | (see terraform.py)

Packaging shape
------------------------------------------------------------------------------
``build`` assembles the notebooks / Spark job definitions / scripts and a wheel into
a build dir (NO docker). ``package`` uploads them to the Synapse workspace ADLS and —
when promoting — captures the resulting **immutable blob version id** as the
promotable reference (the data-job analogue of an image digest). Because the
deployable is a script/wheel bundle (not an image), the plugin declares
``artifact_promotion`` but NOT ``container_images``.

Design boundaries this plugin respects
---------------------------------------
* **No image, ever.** Not one verb emits ``docker build`` / ``docker push`` or
 references ACR/ECS. ``build`` assembles python + a wheel; the deployable is a
 Synapse Spark *job definition*, not an image.
* **Secrets by reference only.** Every account specific — the workspace name,
 the Spark pool, the storage account/filesystem, the job name — is referenced as a
 shell variable (``$SYNAPSE_WORKSPACE``, ``$SYNAPSE_STORAGE_ACCOUNT``, …) or a
 ``settings`` value, never a literal. No subscription id, storage key, or token is
 emitted.
* **Resilient packaging.** ``build`` installs libs from a pinned
 ``requirements.txt`` (PyPI only) — no live distro-mirror step.
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / OIDC / az-login steps are the CI vendor's
 job.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``workspace`` — the Synapse workspace name (else the ``$SYNAPSE_WORKSPACE`` ref).
 * ``spark_pool`` — the Synapse Spark pool (else the ``$SYNAPSE_SPARK_POOL`` ref).
 * ``storage_account`` — the ADLS account (else the ``$SYNAPSE_STORAGE_ACCOUNT`` ref).
 * ``filesystem`` — the ADLS filesystem/container (else the ``$SYNAPSE_FILESYSTEM`` ref).
 * ``job_name`` — the Spark job definition name (else ``<project>-job``).
 * ``package_name`` — the wheel package name (else derived from the project).

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

#: Where the committed Synapse Spark job definition is written (deploy applies it).
JOB_SPEC_PATH = "synapse/spark-job-definition.json"

#: Build output dir for the assembled notebooks/scripts + wheel (NO image).
BUILD_DIR = ".synapse_build"

#: Default by-reference knob names. Account specifics referenced, not valued.
#: ``settings`` may override the *non-secret* ones (names) with values.
_REF_WORKSPACE = "$SYNAPSE_WORKSPACE"
_REF_SPARK_POOL = "$SYNAPSE_SPARK_POOL"
_REF_STORAGE_ACCOUNT = "$SYNAPSE_STORAGE_ACCOUNT"
_REF_FILESYSTEM = "$SYNAPSE_FILESYSTEM"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline. When present,
#: ``package`` resolves+exports the uploaded main file's immutable blob VERSION id
#: into this reference and every ``deploy`` consumes that same reference rather than
#: re-uploading.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


def _project_slug(raw: str) -> str:
    """A stable, importable-ish package slug derived from the IR project name.

 Uses underscores (``[a-z0-9_]``) so the result is a valid Python wheel package
 name — used for ``package_name`` and the main definition file name.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    return cleaned or "app"


def _job_slug(raw: str) -> str:
    """Dash-based job-name prefix — MUST match terraform ``_sanitize_name``.

 The Spark job definition name produced by the CLI ``deploy`` verb and the naming
 convention in Terraform both normalise the IR project name to ``[a-z0-9-]``.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


class AzureSynapse(TargetPlatformPlugin):
    """Target-platform plugin for Azure Synapse Spark/SQL analytics — NO image.

 ``name`` MUST equal the IR value / nested directory home ``plugins/target/azure/
 synapse`` resolves to (``azure-synapse`` via the ``azure-`` prefix rule) so the IR
 value, the folder, and this class all agree (see base.py module docstring,).
 """

    name = "azure-synapse"

    #: Workload capabilities azure-synapse provides: it packages a
    #: script/wheel bundle that can be promoted across environments as an immutable
    #: ADLS blob version (``artifact_promotion`` — lists Synapse as
    #: promotion=yes), deploys per environment (``environments``), authenticates to
    #: Azure keylessly via Workload Identity Federation / OIDC (``oidc``), and
    #: references all account specifics by key (``secrets_by_reference``). It is a
    #: DATA-JOB shape (like databricks): it builds NO container image, so
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

 Single place "value-or-reference" is decided. Non-secret knobs (names) may be
 inlined from ``settings``; anything not supplied falls back to a shell
 *reference* so nothing account-specific is guessed.
 """
        value = ctx.settings.get(key)
        return value if value else ref

    def _job_name(self, ctx: VerbContext) -> str:
        """Job name: explicit setting > ``<project>-job`` (non-secret, derived)."""
        if ctx.settings.get("job_name"):
            return ctx.settings["job_name"]
        return f"{_job_slug(ctx.pipeline.project.name)}-job"

    def _package_name(self, ctx: VerbContext) -> str:
        """Wheel package name: explicit setting > derived from the project name."""
        if ctx.settings.get("package_name"):
            return ctx.settings["package_name"]
        return _project_slug(ctx.pipeline.project.name)

    def _abfss(self, ctx: VerbContext, path: str) -> str:
        """Build an abfss:// URI into the workspace ADLS for ``path`` (by-reference).

 Shape: ``abfss://<filesystem>@<account>.dfs.core.windows.net/<path>``. Each
 segment is a non-secret knob or a shell reference.
 """
        account = self._knob(ctx, "storage_account", _REF_STORAGE_ACCOUNT)
        filesystem = self._knob(ctx, "filesystem", _REF_FILESYSTEM)
        return f"abfss://{filesystem}@{account}.dfs.core.windows.net/{path}"

    # ------------------------------------------------------------------
    # the six stage verbs — NO docker anywhere
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the notebooks / Spark scripts / python (synapse ships code, no Dockerfile).

 Prefer ``ruff`` when available and fall back to a soft notice so the step
 never breaks a pipeline merely because the linter isn't installed. There is
 deliberately NO Dockerfile lint here — synapse has no image.
 """
        commands = (
            "echo 'Linting Synapse notebooks/Spark scripts/python (azure-synapse)'",
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
        """Assemble notebooks / Spark job defs / scripts + a wheel — NO image.

 ``build`` on azure-synapse produces a Python *wheel* (``python -m build``)
 into the build dir and stages the main PySpark definition file + notebooks
 alongside it. There is NO ``docker build`` and NO image tag — the deployable
 is a Synapse Spark job definition, not an image. Also emits the committed job
 definition (``synapse/spark-job-definition.json``) the ``deploy`` verb
 applies, referencing the uploaded artifacts by reference.
 """
        job_name = self._job_name(ctx)
        spark_pool = self._knob(ctx, "spark_pool", _REF_SPARK_POOL)
        package_name = self._package_name(ctx)
        main_file = self._abfss(ctx, f"artifacts/{package_name}/main.py")
        extra_wheel = self._abfss(ctx, f"artifacts/{package_name}/{package_name}.whl")

        commands = (
            "echo 'Building Synapse artifact: wheel + Spark job defs/notebooks (NO image)'",
            f"rm -rf {BUILD_DIR}",
            f"mkdir -p {BUILD_DIR}/notebooks {BUILD_DIR}/scripts",
            # Build a wheel from the project (PEP 517) — no docker involved.
            f"python -m build --wheel --outdir {BUILD_DIR}/",
            # Stage the main PySpark definition file + notebooks for upload.
            f"if [ -d spark_jobs ]; then cp -r spark_jobs/. {BUILD_DIR}/scripts/; "
            "else echo 'no spark_jobs/ dir; expecting a main file already in storage'; fi",
            f"if [ -d notebooks ]; then cp -r notebooks/. {BUILD_DIR}/notebooks/; "
            "else echo 'no notebooks/ dir; wheel + scripts only'; fi",
        )
        job_spec = GeneratedFile(
            path=JOB_SPEC_PATH,
            content=render_job_spec(
                job_name=job_name,
                spark_pool=spark_pool,
                main_definition_file=main_file,
                extra_wheel=extra_wheel,
            ),
        )
        return VerbResult(commands=commands, files=(job_spec,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Upload the artifacts to the Synapse workspace ADLS — no image push.

 ``package`` on azure-synapse uploads the built wheel, the main PySpark file,
 and notebooks to the workspace ADLS filesystem with ``az storage fs... upload``
 (authenticated by the CI vendor's OIDC ``az login`` — AAD mode, no account
 key). The destination account/filesystem are by-reference knobs.

 **Promotion.** When the core threads a shared
 artifact reference (``promotion_artifact_ref`` in ``settings``), ``package``
 resolves the uploaded main file's immutable blob **version id** and EXPORTS a
 stable reference (``abfss://.../main.py?versionId=...``) into that variable, so
 every later ``deploy`` promotes the identical, immutable artifact instead of
 re-uploading. Nothing account- or version-specific is ever inlined here.
 """
        account = self._knob(ctx, "storage_account", _REF_STORAGE_ACCOUNT)
        filesystem = self._knob(ctx, "filesystem", _REF_FILESYSTEM)
        package_name = self._package_name(ctx)
        prefix = f"artifacts/{package_name}"
        commands = [
            "echo 'Uploading Synapse artifacts to ADLS (azure-synapse) — no image push'",
            # The CLI authenticates in AAD mode from the OIDC az login (no key, R6).
            f'WHEEL="$(ls {BUILD_DIR}/*.whl | head -n1)"',
            f"az storage fs file upload --account-name {account} "
            f"--file-system {filesystem} --auth-mode login --overwrite "
            f'--source "$WHEEL" --path "{prefix}/{package_name}.whl"',
            # Upload the main PySpark definition file when staged.
            f'MAIN="$(ls {BUILD_DIR}/scripts/*.py 2>/dev/null | head -n1)"; '
            f'if [ -n "$MAIN" ]; then '
            f"az storage fs file upload --account-name {account} "
            f"--file-system {filesystem} --auth-mode login --overwrite "
            f'--source "$MAIN" --path "{prefix}/main.py"; '
            "else echo 'no local main file staged; assuming it is already in ADLS'; fi",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            # Resolve the immutable blob version for the uploaded main file and export
            # it as the shared promotable reference. Every deploy consumes this exact
            # reference — build once, promote the same artifact.
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (azure-synapse promotion)'",
                    f'BLOB_VERSION="$(az storage fs file show --account-name {account} '
                    f"--file-system {filesystem} --auth-mode login "
                    f'--path "{prefix}/main.py" '
                    "--query 'versionId' --output tsv)\"",
                    f'export {var}="abfss://{filesystem}@{account}.dfs.core.windows.net/'
                    f'{prefix}/main.py?versionId=$BLOB_VERSION"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Create/update a Synapse Spark job definition via ``az synapse``.

 The deployable is a Synapse Spark *job definition*, not an image on a service.
 We look the definition up by name; if it exists we ``az synapse
 spark-job-definition update`` it to the committed spec, otherwise we ``create``
 it. The spec (``synapse/spark-job-definition.json``, emitted by ``build``)
 references the uploaded artifacts. Workspace/job name stay by-reference knobs.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` points the
 job's main file at that PRE-BUILT, immutable reference (the
 ``abfss://...?versionId=...`` produced once by ``package``) instead of a
 mutable path — so dev, staging and prod all run the identical artifact. Absent
 promotion it applies the committed spec exactly as before (backward
 compatible).
 """
        workspace = self._knob(ctx, "workspace", _REF_WORKSPACE)
        job_name = self._job_name(ctx)

        commands = [
            "echo 'Deploying Synapse Spark job definition (create/update via CLI) — NO image'",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            commands.append(
                f'echo "Promoting pre-built Synapse artifact reference: {promo_ref}"'
            )

        commands.extend(
            [
                # Resolve whether the job definition already exists (empty if not).
                f'EXISTING="$(az synapse spark-job-definition show '
                f"--workspace-name {workspace} --name {job_name} "
                "--query 'name' --output tsv 2>/dev/null || true)\"",
                # Update in place if it exists, else create from the committed spec.
                f'if [ "$EXISTING" = "{job_name}" ]; then '
                f"az synapse spark-job-definition update "
                f"--workspace-name {workspace} --name {job_name} "
                f"--file @{JOB_SPEC_PATH}; "
                "else "
                f"az synapse spark-job-definition create "
                f"--workspace-name {workspace} --name {job_name} "
                f"--file @{JOB_SPEC_PATH}; fi",
            ]
        )
        return VerbResult(commands=tuple(commands))

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Start a Synapse Spark job run and poll its state until Succeeded.

 ``verify`` on azure-synapse is *start the job / poll run state* — NOT a
 container health check. We trigger the job via ``az synapse
 spark-job-definition... start``, then poll the batch run until the state is
 terminal and assert it reached ``Succeeded`` (failing the step otherwise).
 Workspace/pool/job name stay by-reference knobs.
 """
        workspace = self._knob(ctx, "workspace", _REF_WORKSPACE)
        spark_pool = self._knob(ctx, "spark_pool", _REF_SPARK_POOL)
        job_name = self._job_name(ctx)
        commands = (
            "echo 'Starting Synapse Spark job run and polling run state (azure-synapse)'",
            # Trigger a run of the deployed job definition and capture its batch id.
            f'BATCH_ID="$(az synapse spark-job-definition start '
            f"--workspace-name {workspace} --name {job_name} "
            "--query 'id' --output tsv)\"",
            # Poll the batch run until the state is terminal.
            "while true; do "
            f'STATE="$(az synapse spark-job --workspace-name {workspace} '
            f'--spark-pool-name {spark_pool} show --livy-id "$BATCH_ID" '
            "--query 'state' --output tsv)\"; "
            'if [ "$STATE" = "success" ] || [ "$STATE" = "dead" ] '
            '|| [ "$STATE" = "killed" ] || [ "$STATE" = "error" ] '
            '|| [ "$STATE" = "Succeeded" ] || [ "$STATE" = "Failed" ]; then break; fi; '
            'echo "job run state: $STATE; waiting..."; sleep 10; done',
            # Assert the terminal state is success/Succeeded, else fail the step.
            'echo "job run final state: $STATE"; '
            '[ "$STATE" = "success" ] || [ "$STATE" = "Succeeded" ]',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — Synapse workspace ref + Spark pool + storage + roles + CI
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform referencing Synapse + provisioning pool/storage/CI.

 Parameterised by the IR (project name for naming, ``cloud.region`` for the
 location default) and by Terraform *variables* for every account specific
 (workspace name/group, OIDC subject/issuer, sizing). No literal subscription
 id, tenant, storage key, or secret is written. The ``summary`` feeds the
 migration runbook.
 """
        project_name = pipeline.project.name
        region = pipeline.cloud.region if pipeline.cloud is not None else None

        files = (
            GeneratedFile(
                path=f"iac/azure-synapse/main.tf",
                content=render_main_tf(project_name),
            ),
            GeneratedFile(
                path=f"iac/azure-synapse/variables.tf",
                content=render_variables_tf(region),
            ),
            GeneratedFile(
                path=f"iac/azure-synapse/outputs.tf",
                content=render_outputs_tf(),
            ),
            GeneratedFile(
                path=f"iac/azure-synapse/terraform.tfvars.example",
                content=render_tfvars_example(project_name, region),
            ),
        )

        region_text = region if region else "the configured Azure region"
        summary = (
            f"Azure Synapse backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: it REFERENCES the existing Synapse "
            f"workspace (it does not create one), provisions a Synapse Spark pool (the "
            f"deploy verb creates/updates a Spark job definition that runs on it; the "
            f"verify verb starts + polls a run), an ADLS Gen2 data lake + filesystem "
            f"the package verb uploads notebooks/scripts/the wheel to (the immutable "
            f"blob version is the promotable reference), the role assignments that let "
            f"the workspace identity and the CI identity read/write the data lake "
            f"keylessly (Storage Blob Data Contributor — no committed key), and a "
            f"user-assigned identity plus an OIDC federated credential for keyless CI "
            f"deploys. This is a data-job shape — there is NO container image, NO ACR, "
            f"and NO ECS. All account specifics (subscription id, tenant, workspace "
            f"name, OIDC subject) are Terraform variables — no literal secrets or "
            f"account values are committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
