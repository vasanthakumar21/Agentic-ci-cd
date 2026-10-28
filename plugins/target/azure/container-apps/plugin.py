"""Azure Container Apps target-platform plugin.

Implements the six stage verbs for the *azure-container-apps* platform and supplies
the backing Terraform. The verb meaning is the azure-container-apps row:

 | verb | azure-container-apps |
 |---------|-------------------------------------------------------|
 | lint | lint the Dockerfile | (generic/hadolint, keep reasonable)
 | test | run the project's tests | (from stage.runs, else a placeholder)
 | build | docker build | (+ emits a resilient Dockerfile,)
 | package | push image to ACR | (immutable digest = promotable reference)
 | deploy | `az containerapp update --image` (pre-built image) | (immutable digest under promotion)
 | verify | poll the latest revision provisioningState/health |
 | IaC | Container App + environment + ACR + identity (TF) | (see terraform.py)

Container Apps is container-based like Cloud Run / aws-ecs — ``build`` is ``docker
build`` and the deployable is a container image — but the registry is **Azure
Container Registry (ACR)** and the deploy is **``az containerapp update``** (a
fully-managed service, no cluster). Keyless CI auth uses **Azure Workload Identity
Federation / OIDC** (``az login`` is the CI vendor's step). The plugin is
self-contained — it carries its own resilient Dockerfile and does not import across
plugin boundaries.

Design boundaries this plugin respects
---------------------------------------
* **Secrets by reference only.** Every account/environment specific — the ACR
 login server / name, the image ref, the Container App name, the resource group —
 is referenced as a shell variable (``$ACR_LOGIN_SERVER``, ``$CONTAINER_APP``, …)
 or a ``settings`` value, never a literal. No subscription id, tenant, or token is
 emitted.
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / OIDC / az-login steps are the CI vendor's
 job. A verb never assumes a particular CI vendor.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``acr_login_server`` — the ACR login server (else the ``$ACR_LOGIN_SERVER`` ref).
 * ``acr_name`` — the ACR registry name (else the ``$ACR_NAME`` ref).
 * ``image_name`` — the image name within the registry (else ``$IMAGE_NAME``).
 * ``image_tag`` — the image tag (else the ``$IMAGE_TAG`` ref).
 * ``container_app`` — the Container App name (else the ``$CONTAINER_APP`` ref).
 * ``resource_group`` — the resource group (else the ``$AZURE_RESOURCE_GROUP`` ref).

Nothing in ``settings`` is a secret; secrets remain references.
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

from .dockerfile import DOCKERFILE_CONTENT
from .terraform import (
    render_main_tf,
    render_outputs_tf,
    render_tfvars_example,
    render_variables_tf,
)

#: Where the resilient Dockerfile is written, relative to the target repo root.
DOCKERFILE_PATH = "Dockerfile"

#: Where the Terraform module is written (one dir per target platform,).
IAC_DIR = "iac/azure-container-apps"

#: Default by-reference knob names. Account/environment specifics referenced, not
#: valued. ``settings`` may override the *non-secret* ones (names) with values.
_REF_ACR_LOGIN_SERVER = "$ACR_LOGIN_SERVER"
_REF_ACR_NAME = "$ACR_NAME"
_REF_IMAGE_NAME = "$IMAGE_NAME"
_REF_IMAGE_TAG = "$IMAGE_TAG"
_REF_CONTAINER_APP = "$CONTAINER_APP"
_REF_RESOURCE_GROUP = "$AZURE_RESOURCE_GROUP"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline. When
#: present, ``package`` resolves+exports the pushed image DIGEST into this reference
#: and every ``deploy`` consumes that same reference rather than rebuilding.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


class AzureContainerApps(TargetPlatformPlugin):
    """Target-platform plugin for Azure Container Apps over a Docker image.

 ``name`` MUST equal the IR value / nested directory home
 ``plugins/target/azure/container-apps`` resolves to (``azure-container-apps`` via
 the ``azure-`` prefix rule, splitting on the FIRST ``-`` so the multi-hyphen
 service name ``container-apps`` is preserved) so the IR value, the folder, and
 this class all agree (see base.py module docstring,).
 """

    name = "azure-container-apps"

    #: Workload capabilities azure-container-apps provides: it
    #: builds+pushes a container image (``container_images``) that can be promoted
    #: across environments as an immutable digest reference (``artifact_promotion``),
    #: deploys per environment (``environments``), authenticates to Azure keylessly
    #: via Workload Identity Federation / OIDC (``oidc``), and references all account
    #: specifics by key (``secrets_by_reference``).
    capabilities = frozenset(
        {
            Capability.CONTAINER_IMAGES,
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
 (registry/app/group names) may be inlined from ``settings``; anything not
 supplied falls back to a shell *reference* so no account/environment specific
 is ever guessed or hard-coded.
 """
        value = ctx.settings.get(key)
        return value if value else ref

    def _image_reference(self, ctx: VerbContext) -> str:
        """Build the fully-qualified ACR image ref (by-reference, R6).

 Shape: ``<login_server>/<image>:<tag>``. Each segment is a non-secret knob or
 a shell reference, so no registry literal is baked in unless the client
 supplied it via ``settings``.
 """
        login_server = self._knob(ctx, "acr_login_server", _REF_ACR_LOGIN_SERVER)
        image = self._knob(ctx, "image_name", _REF_IMAGE_NAME)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        return f"{login_server}/{image}:{tag}"

    # ------------------------------------------------------------------
    # the six stage verbs
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the Dockerfile (azure-container-apps ships a container image).

 Container Apps builds an image, so ``lint`` statically checks the Dockerfile.
 We prefer ``hadolint`` when available and fall back to a no-fail notice so the
 step never breaks a pipeline merely because the linter isn't installed — the
 intent (lint the image definition) is expressed either way.
 """
        commands = (
            "echo 'Linting Dockerfile (azure-container-apps)'",
            "if command -v hadolint >/dev/null 2>&1; then "
            f"hadolint {DOCKERFILE_PATH}; "
            "else echo 'hadolint not found; skipping Dockerfile lint'; fi",
        )
        return VerbResult(commands=commands)

    def test(self, ctx: VerbContext) -> VerbResult:
        """Run the project's test suite.

 Testing is project-defined, so we honour ``stage.runs`` when the IR sets it
 and fall back to a conventional ``make test`` placeholder otherwise. The
 plugin does not impose a test runner — that is the client's choice.
 """
        run_cmd = ctx.stage.runs if ctx.stage.runs is not None else "make test"
        return VerbResult(commands=(run_cmd,))

    def build(self, ctx: VerbContext) -> VerbResult:
        """``docker build`` — and emit the resilient Dockerfile.

 Container Apps runs a container image, so ``build`` is ``docker build``
 exactly like Cloud Run. The image is tagged with the fully-qualified ACR
 reference using by-reference knobs so no account specific is baked in.
 The build also emits a pinned, multi-stage Dockerfile with NO live apt-mirror
 dependency (see :mod:`.dockerfile`) so the build cannot go red because a
 distro mirror is down — that is the whole point of.
 """
        image = self._image_reference(ctx)
        commands = (
            "echo 'Building container image (azure-container-apps)'",
            f"docker build -t {image} -f {DOCKERFILE_PATH} .",
        )
        dockerfile = GeneratedFile(path=DOCKERFILE_PATH, content=DOCKERFILE_CONTENT)
        return VerbResult(commands=commands, files=(dockerfile,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Push the image to ACR (``package`` = push image to ACR,).

 Authenticates to the Azure Container Registry with the keyless, federated
 credentials (``az acr login`` using the token the CI vendor's OIDC
 ``az login`` established) — no static credentials — then pushes the tagged
 image. The registry name is derived from the by-reference knobs.

 **Promotion.** When the core threads a
 shared artifact reference (``promotion_artifact_ref`` in ``settings``),
 ``package`` resolves the pushed image's immutable DIGEST (``az acr repository
 show``) and EXPORTS it into that reference variable, so every later ``deploy``
 promotes the identical, immutable artifact (``login_server/image@sha256:...``)
 instead of rebuilding. The digest is resolved at runtime; nothing account- or
 digest-specific is ever inlined here.
 """
        acr_name = self._knob(ctx, "acr_name", _REF_ACR_NAME)
        image = self._image_reference(ctx)
        image_name = self._knob(ctx, "image_name", _REF_IMAGE_NAME)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        commands = [
            "echo 'Pushing image to Azure Container Registry (azure-container-apps)'",
            # az acr login uses the federated token established by the CI OIDC login.
            f"az acr login --name {acr_name}",
            f"docker push {image}",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            # Resolve the immutable digest for the just-pushed tag and export it as
            # the shared promotable reference (login_server/image@sha256:...). Every
            # deploy will consume this exact reference — build once, promote the same
            # image.
            login_server = self._knob(ctx, "acr_login_server", _REF_ACR_LOGIN_SERVER)
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (azure-container-apps promotion)'",
                    f"IMAGE_DIGEST=\"$(az acr repository show "
                    f"--name {acr_name} --image {image_name}:{tag} "
                    "--query 'digest' --output tsv)\"",
                    f'export {var}="{login_server}/{image_name}@$IMAGE_DIGEST"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Deploy with ``az containerapp update --image``.

 Updates the Container App to run the pre-built image with ``az containerapp
 update --image``, which creates a new revision. The app name, resource group,
 and image are by-reference knobs; credentials (the federated access
 token) are the CI vendor's step, never emitted here.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` consumes
 that PRE-BUILT reference (the immutable ``login_server/image@sha256:...``
 digest produced once by ``package``) as the ``--image`` argument, so every
 environment rolls out the identical artifact rather than rebuilding/re-tagging.
 Absent promotion it falls back to the fully-qualified ``login_server/image:tag``
 reference exactly as before (backward compatible).
 """
        app = self._knob(ctx, "container_app", _REF_CONTAINER_APP)
        group = self._knob(ctx, "resource_group", _REF_RESOURCE_GROUP)

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            # Deploy the pre-built, immutable reference (no rebuild) —
            image = promo_ref
        else:
            image = self._image_reference(ctx)

        commands = (
            "echo 'Deploying to Azure Container Apps (azure-container-apps)'",
            f"az containerapp update --name {app} "
            f"--resource-group {group} --image {image}",
        )
        return VerbResult(commands=commands)

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Verify the latest revision is provisioned/healthy.

 Polls the Container App's latest revision ``provisioningState`` until it
 reports ``Provisioned`` and asserts the revision is healthy, then prints the
 app's ingress FQDN. The app name and resource group are by-reference knobs.
 """
        app = self._knob(ctx, "container_app", _REF_CONTAINER_APP)
        group = self._knob(ctx, "resource_group", _REF_RESOURCE_GROUP)
        commands = (
            "echo 'Verifying Container Apps latest revision health (azure-container-apps)'",
            # Resolve the latest revision name.
            f"REVISION=\"$(az containerapp show --name {app} "
            f"--resource-group {group} "
            "--query 'properties.latestRevisionName' --output tsv)\"",
            'echo "Latest revision: $REVISION"',
            # Poll the revision provisioningState until it is terminal.
            "while true; do "
            f'STATE="$(az containerapp revision show --name {app} '
            f'--resource-group {group} --revision "$REVISION" '
            "--query 'properties.provisioningState' --output tsv)\"; "
            'if [ "$STATE" = "Provisioned" ] || [ "$STATE" = "Failed" ]; then break; fi; '
            'echo "revision provisioningState: $STATE; waiting..."; sleep 10; done',
            # Assert the revision provisioned and is healthy, else fail the step.
            'echo "revision provisioningState: $STATE"; [ "$STATE" = "Provisioned" ]',
            f"HEALTH=\"$(az containerapp revision show --name {app} "
            f'--resource-group {group} --revision "$REVISION" '
            "--query 'properties.healthState' --output tsv)\"",
            'echo "revision healthState: $HEALTH"; '
            '[ "$HEALTH" = "Healthy" ] || [ "$HEALTH" = "None" ]',
            f"az containerapp show --name {app} --resource-group {group} "
            "--query 'properties.configuration.ingress.fqdn' --output tsv",
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — Container App + environment + ACR + identity Terraform
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning Container App + env + ACR + identity.

 Parameterised by the IR (project name for naming, ``cloud.region`` for the
 location default) and by Terraform *variables* for every account specific
 (resource group, OIDC subject/issuer, scaling). No literal subscription id,
 tenant, or secret is written — CI authenticates keylessly by federating
 its OIDC token into a CI identity. The ``summary`` feeds the migration runbook.
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
            f"Azure Container Apps backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: a resource group, an Azure Container "
            f"Registry (ACR) for the built image, a Log Analytics workspace, a "
            f"Container Apps environment, the Container App itself (the deploy verb "
            f"updates its image; the verify verb polls its latest revision), a "
            f"user-assigned managed identity for keyless ACR pulls (AcrPull), and a "
            f"user-assigned identity plus an OIDC federated credential for keyless CI "
            f"deploys (AcrPush + Contributor on the app, no client secret). This is a "
            f"managed container service — there is NO cluster to manage. All account "
            f"specifics (subscription id, tenant, OIDC subject) are Terraform "
            f"variables — no literal secrets or account values are committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
