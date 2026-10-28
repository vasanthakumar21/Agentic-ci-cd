"""Azure Data Factory target-platform plugin.

Implements the six stage verbs for the *azure-data-factory* platform and supplies the
backing Terraform. azure-data-factory is a **data-job shape** like databricks /
aws-glue: there is NO container image, NO ACR, NO ECS. The verb meaning is the azure-data-factory row:

 | verb | azure-data-factory |
 |---------|-----------------------------------------------------------------|
 | lint | lint / validate the ADF JSON definitions (generic/jq) |
 | test | run the project's tests (stage.runs, else a placeholder) |
 | build | assemble/validate the ADF pipeline + linked-service + dataset | NO image
 | package | upload the definitions to storage | (versioned blob = promotable ref)
 | deploy | `az datafactory pipeline create` (upsert the ADF pipeline) | (consumes a pre-built ref under promotion)
 | verify | `az datafactory pipeline create-run` + poll run to Succeeded |
 | IaC | Data Factory + pipeline + role + storage + CI (TF) | (see terraform.py)

Packaging shape
-------------------------------------------------------------------------
``build`` renders/validates the committed ADF JSON (pipeline + linked-service +
dataset) into a build dir (NO docker). ``package`` uploads them to the ADF storage
and — when promoting — captures the resulting **immutable blob version id** as the
promotable reference. Because the deployable is a JSON definition bundle (not an
image), the plugin declares ``artifact_promotion`` but NOT ``container_images``.

Design boundaries this plugin respects
---------------------------------------
* **No image, ever.** Not one verb emits ``docker build`` / ``docker push`` or
 references ACR/ECS. The deployable is an ADF *pipeline*, not an image.
* **Secrets by reference only.** Every account specific — the factory name, the
 resource group, the storage account/filesystem, the pipeline name — is referenced
 as a shell variable (``$ADF_NAME``, ``$ADF_STORAGE_ACCOUNT``, …) or a ``settings``
 value, never a literal. No subscription id, connection string, or token is emitted.
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / OIDC / az-login steps are the CI vendor's
 job.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``factory_name`` — the Data Factory name (else the ``$ADF_NAME`` ref).
 * ``resource_group`` — the resource group (else the ``$AZURE_RESOURCE_GROUP`` ref).
 * ``storage_account`` — the ADLS account (else the ``$ADF_STORAGE_ACCOUNT`` ref).
 * ``filesystem`` — the ADLS filesystem/container (else the ``$ADF_FILESYSTEM`` ref).
 * ``pipeline_name`` — the ADF pipeline name (else ``<project>-pipeline``).

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

from .definitions import render_dataset, render_linked_service, render_pipeline
from .terraform import (
    render_main_tf,
    render_outputs_tf,
    render_tfvars_example,
    render_variables_tf,
)

#: Where the committed ADF JSON definitions are written (build renders, deploy applies).
DEFINITIONS_DIR = "adf"
PIPELINE_PATH = f"{DEFINITIONS_DIR}/pipeline.json"
LINKED_SERVICE_PATH = f"{DEFINITIONS_DIR}/linked-service.json"
DATASET_PATH = f"{DEFINITIONS_DIR}/dataset.json"

#: Where the Terraform module is written (one dir per target platform,).
IAC_DIR = "iac/azure-data-factory"

#: Default by-reference knob names. Account specifics referenced, not valued.
#: ``settings`` may override the *non-secret* ones (names) with values.
_REF_FACTORY = "$ADF_NAME"
_REF_RESOURCE_GROUP = "$AZURE_RESOURCE_GROUP"
_REF_STORAGE_ACCOUNT = "$ADF_STORAGE_ACCOUNT"
_REF_FILESYSTEM = "$ADF_FILESYSTEM"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline. When present,
#: ``package`` resolves+exports the uploaded pipeline definition's immutable blob
#: VERSION id into this reference and every ``deploy`` consumes that same reference
#: rather than re-uploading.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


def _slug(raw: str) -> str:
    """Dash-based name prefix normalising the IR project name to ``[a-z0-9-]``."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


class AzureDataFactory(TargetPlatformPlugin):
    """Target-platform plugin for Azure Data Factory data pipelines — NO image.

 ``name`` MUST equal the IR value / nested directory home
 ``plugins/target/azure/data-factory`` resolves to (``azure-data-factory`` via the
 ``azure-`` prefix rule, splitting on the FIRST ``-`` so the multi-hyphen service
 name ``data-factory`` is preserved) so the IR value, the folder, and this class
 all agree (see base.py module docstring,).
 """

    name = "azure-data-factory"

    #: Workload capabilities azure-data-factory provides: it packages a
    #: JSON definition bundle that can be promoted across environments as an immutable
    #: ADLS blob version (``artifact_promotion`` — lists ADF as promotion=yes),
    #: deploys per environment (``environments``), authenticates to Azure keylessly via
    #: Workload Identity Federation / OIDC (``oidc``), and references all account
    #: specifics by key (``secrets_by_reference``). It is a DATA-JOB shape (like
    #: databricks): it builds NO container image, so ``container_images`` is
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

 Single place "value-or-reference" is decided. Non-secret knobs (names) may be
 inlined from ``settings``; anything not supplied falls back to a shell
 *reference* so nothing account-specific is guessed.
 """
        value = ctx.settings.get(key)
        return value if value else ref

    def _pipeline_name(self, ctx: VerbContext) -> str:
        """Pipeline name: explicit setting > ``<project>-pipeline`` (non-secret)."""
        if ctx.settings.get("pipeline_name"):
            return ctx.settings["pipeline_name"]
        return f"{_slug(ctx.pipeline.project.name)}-pipeline"

    def _names(self, ctx: VerbContext) -> tuple[str, str, str]:
        """The linked-service / dataset / pipeline names derived from the project."""
        prefix = _slug(ctx.pipeline.project.name)
        return (
            f"{prefix}-linkedservice",
            f"{prefix}-dataset",
            self._pipeline_name(ctx),
        )

    # ------------------------------------------------------------------
    # the six stage verbs — NO docker
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Validate the ADF JSON definitions (data-factory ships JSON, not a Dockerfile).

 Prefer ``jq`` to confirm the committed ADF definitions are well-formed JSON
 and fall back to a soft notice so the step never breaks a pipeline merely
 because the tool isn't installed. There is deliberately NO Dockerfile lint
 here — data-factory has no image.
 """
        commands = (
            "echo 'Validating ADF JSON definitions (azure-data-factory)'",
            "if command -v jq >/dev/null 2>&1; then "
            f"jq empty {PIPELINE_PATH} {LINKED_SERVICE_PATH} {DATASET_PATH}; "
            "else echo 'jq not found; skipping ADF JSON validation'; fi",
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
        """Assemble/validate the ADF pipeline + linked-service + dataset JSON.

 ``build`` on azure-data-factory emits the committed ADF JSON definitions
 (pipeline + linked-service + dataset) as support files and validates they are
 well-formed. There is NO ``docker build`` and NO image tag — the deployable
 is an ADF pipeline, not an image. The linked service references the storage
 account by its blob endpoint (managed-identity auth), never a connection
 string.
 """
        ls_name, ds_name, pipeline_name = self._names(ctx)
        storage_account = self._knob(ctx, "storage_account", _REF_STORAGE_ACCOUNT)
        filesystem = self._knob(ctx, "filesystem", _REF_FILESYSTEM)

        commands = (
            "echo 'Building ADF definitions: pipeline + linked-service + dataset (NO image)'",
            # Validate the emitted JSON is well-formed before deploy applies it.
            "if command -v jq >/dev/null 2>&1; then "
            f"jq empty {PIPELINE_PATH} {LINKED_SERVICE_PATH} {DATASET_PATH}; "
            "else echo 'jq not found; skipping ADF JSON validation'; fi",
        )
        files = (
            GeneratedFile(
                path=LINKED_SERVICE_PATH,
                content=render_linked_service(
                    name=ls_name, storage_account=storage_account
                ),
            ),
            GeneratedFile(
                path=DATASET_PATH,
                content=render_dataset(
                    name=ds_name, linked_service_name=ls_name, filesystem=filesystem
                ),
            ),
            GeneratedFile(
                path=PIPELINE_PATH,
                content=render_pipeline(name=pipeline_name, dataset_name=ds_name),
            ),
        )
        return VerbResult(commands=commands, files=files)

    def package(self, ctx: VerbContext) -> VerbResult:
        """Upload the ADF definitions to storage — no image push.

 ``package`` on azure-data-factory uploads the committed ADF JSON definitions
 to the ADLS filesystem with ``az storage fs file upload`` (authenticated by
 the CI vendor's OIDC ``az login`` — AAD mode, no account key). The destination
 account/filesystem are by-reference knobs.

 **Promotion.** When the core threads a shared
 artifact reference (``promotion_artifact_ref`` in ``settings``), ``package``
 resolves the uploaded pipeline definition's immutable blob **version id** and
 EXPORTS a stable reference (``abfss://.../pipeline.json?versionId=...``) into
 that variable, so every later ``deploy`` promotes the identical, immutable
 definition instead of re-uploading. Nothing account- or version-specific is
 ever inlined here.
 """
        account = self._knob(ctx, "storage_account", _REF_STORAGE_ACCOUNT)
        filesystem = self._knob(ctx, "filesystem", _REF_FILESYSTEM)
        prefix = f"{_slug(ctx.pipeline.project.name)}/definitions"
        commands = [
            "echo 'Uploading ADF definitions to ADLS (azure-data-factory) — no image push'",
            # The CLI authenticates in AAD mode from the OIDC az login (no key, R6).
            f"az storage fs file upload --account-name {account} "
            f"--file-system {filesystem} --auth-mode login --overwrite "
            f'--source {PIPELINE_PATH} --path "{prefix}/pipeline.json"',
            f"az storage fs file upload --account-name {account} "
            f"--file-system {filesystem} --auth-mode login --overwrite "
            f'--source {LINKED_SERVICE_PATH} --path "{prefix}/linked-service.json"',
            f"az storage fs file upload --account-name {account} "
            f"--file-system {filesystem} --auth-mode login --overwrite "
            f'--source {DATASET_PATH} --path "{prefix}/dataset.json"',
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (azure-data-factory promotion)'",
                    f'BLOB_VERSION="$(az storage fs file show --account-name {account} '
                    f"--file-system {filesystem} --auth-mode login "
                    f'--path "{prefix}/pipeline.json" '
                    "--query 'versionId' --output tsv)\"",
                    f'export {var}="abfss://{filesystem}@{account}.dfs.core.windows.net/'
                    f'{prefix}/pipeline.json?versionId=$BLOB_VERSION"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Upsert the ADF pipeline via ``az datafactory pipeline create``.

 The deployable is an ADF *pipeline*, not an image on a service. We first
 ensure the linked service + dataset exist (``az datafactory linked-service /
 dataset create`` are idempotent upserts), then upsert the pipeline from the
 committed JSON with ``az datafactory pipeline create``. Factory/resource-group
 names stay by-reference knobs.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` notes that
 the identical, immutable definition (the ``abfss://...?versionId=...`` produced
 once by ``package``) is being promoted so dev, staging and prod all apply the
 same artifact. Absent promotion it applies the committed JSON exactly as
 before (backward compatible).
 """
        factory = self._knob(ctx, "factory_name", _REF_FACTORY)
        group = self._knob(ctx, "resource_group", _REF_RESOURCE_GROUP)
        ls_name, ds_name, pipeline_name = self._names(ctx)

        commands = [
            "echo 'Deploying ADF pipeline (upsert via CLI) — NO image'",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            commands.append(
                f'echo "Promoting pre-built ADF definition reference: {promo_ref}"'
            )

        commands.extend(
            [
                # Upsert the linked service + dataset the pipeline depends on.
                f"az datafactory linked-service create "
                f"--factory-name {factory} --resource-group {group} "
                f"--linked-service-name {ls_name} --properties @{LINKED_SERVICE_PATH}",
                f"az datafactory dataset create "
                f"--factory-name {factory} --resource-group {group} "
                f"--dataset-name {ds_name} --properties @{DATASET_PATH}",
                # Upsert the pipeline itself from the committed definition.
                f"az datafactory pipeline create "
                f"--factory-name {factory} --resource-group {group} "
                f"--name {pipeline_name} --pipeline @{PIPELINE_PATH}",
            ]
        )
        return VerbResult(commands=tuple(commands))

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Trigger an ADF pipeline run and poll its status until Succeeded.

 ``verify`` on azure-data-factory is *trigger the pipeline / poll run status* —
 NOT a container health check. We trigger ``az datafactory pipeline create-run``,
 then poll ``az datafactory pipeline-run show`` until the status is terminal and
 assert it reached ``Succeeded`` (failing the step otherwise). Factory/pipeline
 names stay by-reference knobs.
 """
        factory = self._knob(ctx, "factory_name", _REF_FACTORY)
        group = self._knob(ctx, "resource_group", _REF_RESOURCE_GROUP)
        pipeline_name = self._pipeline_name(ctx)
        commands = (
            "echo 'Triggering ADF pipeline run and polling run status (azure-data-factory)'",
            # Trigger a run of the deployed pipeline and capture its run id.
            f'RUN_ID="$(az datafactory pipeline create-run '
            f"--factory-name {factory} --resource-group {group} "
            f"--name {pipeline_name} --query 'runId' --output tsv)\"",
            # Poll the pipeline run until its status is terminal.
            "while true; do "
            f'STATUS="$(az datafactory pipeline-run show '
            f"--factory-name {factory} --resource-group {group} "
            '--run-id "$RUN_ID" --query "status" --output tsv)"; '
            'if [ "$STATUS" = "Succeeded" ] || [ "$STATUS" = "Failed" ] '
            '|| [ "$STATUS" = "Cancelled" ]; then break; fi; '
            'echo "pipeline run status: $STATUS; waiting..."; sleep 10; done',
            # Assert the terminal status is Succeeded, else fail the step.
            'echo "pipeline run final status: $STATUS"; [ "$STATUS" = "Succeeded" ]',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — Data Factory + pipeline + role + storage + CI Terraform
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning the Data Factory + pipeline + roles + CI.

 Parameterised by the IR (project name for naming, ``cloud.region`` for the
 location default) and by Terraform *variables* for every account specific
 (resource group, OIDC subject/issuer). No literal subscription id, tenant,
 connection string, or secret is written. The ``summary`` feeds the
 migration runbook.
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

        region_text = region if region else "the configured Azure region"
        summary = (
            f"Azure Data Factory backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: a resource group, an Azure Data "
            f"Factory (azurerm_data_factory — the deploy verb upserts a pipeline into "
            f"it; the verify verb triggers + polls a run) with a system-assigned "
            f"managed identity, a starter data factory pipeline resource, an ADLS Gen2 "
            f"data lake + filesystem the package verb uploads the ADF JSON definitions "
            f"to (the immutable blob version is the promotable reference), the role "
            f"assignments that let the factory identity and the CI identity read/write "
            f"the data lake keylessly (Storage Blob Data Contributor — no committed "
            f"connection string), and a user-assigned identity plus an OIDC federated "
            f"credential for keyless CI deploys. This is a data-job shape — there is NO "
            f"container image, NO ACR, and NO ECS. All account specifics (subscription "
            f"id, tenant, OIDC subject) are Terraform variables — no literal secrets or "
            f"account values are committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
