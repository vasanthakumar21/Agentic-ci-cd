"""Azure AKS target-platform plugin.

Implements the six stage verbs for the *azure-aks* platform and supplies the backing
Terraform + manifests. The verb meaning is the azure-aks row:

 | verb | azure-aks |
 |---------|-----------------------------------------------------------------|
 | lint | lint the Dockerfile + manifests | (generic/hadolint, keep reasonable)
 | test | run the project's tests | (from stage.runs, else a placeholder)
 | build | docker build | (+ emits a resilient Dockerfile,)
 | package | push image to ACR | (immutable digest = promotable reference)
 | deploy | `az aks get-credentials` then `kubectl apply` / `helm upgrade` | (consumes the pre-built digest under promotion)
 | verify | `kubectl rollout status` |
 | IaC | AKS refs + ACR + workload identity + federated credential (TF) | (see terraform.py / manifests.py)

AKS is the Azure analogue of aws-eks: it is **container-based** (``build`` =
``docker build``, ``package`` = push to ACR, and the immutable image DIGEST is the
promotable reference) but it **deploys like the generic kubernetes plugin**
(``kubectl``/``helm`` apply, ``kubectl rollout status`` to verify). Keyless CI auth
uses **Azure Workload Identity Federation / OIDC** (``az login --federated-token``
is the CI vendor's step); the registry is **Azure Container Registry (ACR)**. The
plugin is self-contained — it carries its own resilient Dockerfile and manifests and
does not import across plugin boundaries.

Design boundaries this plugin respects
---------------------------------------
* **Secrets by reference only.** Every account/environment specific — the ACR
 login server, the AKS cluster name, the resource group, the namespace, the
 kubeconfig context, the deployment name — is referenced as a shell variable
 (``$ACR_LOGIN_SERVER``, ``$AKS_CLUSTER``, ``$NAMESPACE``, …) or a ``settings``
 value, never a literal. No subscription id, tenant, kubeconfig, or token is
 emitted. Pod identity comes from Azure Workload Identity (the ServiceAccount's
 ``azure.workload.identity/client-id`` annotation, filled at deploy).
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / OIDC credential / az-login steps are the
 CI vendor's job. A verb never assumes a particular CI vendor.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``acr_login_server`` — the ACR login server (else the ``$ACR_LOGIN_SERVER`` ref).
 * ``acr_name`` — the ACR registry name (else the ``$ACR_NAME`` ref).
 * ``image_name`` — the image name within the registry (else ``$IMAGE_NAME``).
 * ``image_tag`` — the image tag (else the ``$IMAGE_TAG`` ref).
 * ``aks_cluster`` — AKS cluster name (else the ``$AKS_CLUSTER`` ref).
 * ``resource_group`` — the resource group (else the ``$AZURE_RESOURCE_GROUP`` ref).
 * ``namespace`` — the target namespace (else the ``$NAMESPACE`` ref).
 * ``deployment_name`` — the Deployment name (else the ``$DEPLOYMENT_NAME`` ref).
 * ``kube_context`` — kubeconfig context (else the ``$KUBE_CONTEXT`` ref).

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
from .manifests import (
    render_deployment_yaml,
    render_service_yaml,
    render_serviceaccount_yaml,
    sanitize_name,
)
from .terraform import (
    render_main_tf,
    render_outputs_tf,
    render_tfvars_example,
    render_variables_tf,
)

#: Where the resilient Dockerfile is written, relative to the target repo root.
DOCKERFILE_PATH = "Dockerfile"

#: Where the IaC (manifests + Terraform) is written (one dir per platform,).
IAC_DIR = "iac/azure-aks"

#: Default by-reference knob names. Account/environment specifics referenced, not
#: valued. ``settings`` may override the *non-secret* ones (names) with values.
_REF_ACR_LOGIN_SERVER = "$ACR_LOGIN_SERVER"
_REF_ACR_NAME = "$ACR_NAME"
_REF_IMAGE_NAME = "$IMAGE_NAME"
_REF_IMAGE_TAG = "$IMAGE_TAG"
_REF_AKS_CLUSTER = "$AKS_CLUSTER"
_REF_RESOURCE_GROUP = "$AZURE_RESOURCE_GROUP"
_REF_NAMESPACE = "$NAMESPACE"
_REF_DEPLOYMENT = "$DEPLOYMENT_NAME"
_REF_KUBE_CONTEXT = "$KUBE_CONTEXT"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline. When present,
#: ``package`` resolves+exports the pushed image DIGEST into this reference and every
#: ``deploy`` consumes that same reference rather than rebuilding.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


class AzureAks(TargetPlatformPlugin):
    """Target-platform plugin for Azure AKS over a Docker image.

 ``name`` MUST equal the IR value / nested directory home ``plugins/target/azure/
 aks`` resolves to (``azure-aks`` via the ``azure-`` prefix rule) so the IR value,
 the folder, and this class all agree (see base.py module docstring,).
 """

    name = "azure-aks"

    #: Workload capabilities azure-aks provides: it builds+pushes a
    #: container image (``container_images``) that can be promoted across
    #: environments as an immutable digest reference (``artifact_promotion``), rolls
    #: out per environment (``environments``), authenticates to Azure keylessly via
    #: Workload Identity Federation / OIDC (``oidc``), and references all account
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
 (registry/cluster/namespace/deployment names) may be inlined from
 ``settings``; anything not supplied falls back to a shell *reference* so no
 account/environment specific is ever guessed or hard-coded.
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
        """Lint the Dockerfile (azure-aks ships a container image, like aws-eks).

 AKS builds an image, so ``lint`` statically checks the Dockerfile. We prefer
 ``hadolint`` when available and fall back to a no-fail notice so the step
 never breaks a pipeline merely because the linter isn't installed — the
 intent (lint the image definition) is expressed either way.
 """
        commands = (
            "echo 'Linting Dockerfile (azure-aks)'",
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

 AKS runs a container image, so ``build`` is ``docker build`` exactly like
 aws-eks. The image is tagged with the fully-qualified ACR reference using
 by-reference knobs so no account specific is baked in. The build also
 emits a pinned, multi-stage Dockerfile with NO live apt-mirror dependency
 (see :mod:`.dockerfile`) so the build cannot go red because a distro mirror
 is down — that is the whole point of.
 """
        image = self._image_reference(ctx)
        commands = (
            "echo 'Building container image (azure-aks)'",
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

 **Promotion.** When the core threads a shared
 artifact reference (``promotion_artifact_ref`` in ``settings``), ``package``
 resolves the pushed image's immutable DIGEST (``az acr repository show``) and
 EXPORTS it into that reference variable, so every later ``deploy`` promotes
 the identical, immutable artifact (``login_server/image@sha256:...``) instead
 of rebuilding. The digest is resolved at runtime; nothing account- or
 digest-specific is ever inlined here.
 """
        acr_name = self._knob(ctx, "acr_name", _REF_ACR_NAME)
        image = self._image_reference(ctx)
        image_name = self._knob(ctx, "image_name", _REF_IMAGE_NAME)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        commands = [
            "echo 'Pushing image to Azure Container Registry (azure-aks)'",
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
            # image. The image path (without the:tag) is the digest reference's
            # left side.
            login_server = self._knob(ctx, "acr_login_server", _REF_ACR_LOGIN_SERVER)
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (azure-aks promotion)'",
                    f"IMAGE_DIGEST=\"$(az acr repository show "
                    f"--name {acr_name} --image {image_name}:{tag} "
                    "--query 'digest' --output tsv)\"",
                    f'export {var}="{login_server}/{image_name}@$IMAGE_DIGEST"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Roll the workload out with ``kubectl apply`` / ``helm upgrade``.

 Refreshes cluster credentials with ``az aks get-credentials`` (keyless, via
 the CI OIDC login), then prefers a Helm release when a chart is present
 (``helm upgrade --install``) and otherwise applies the generated manifests
 with ``kubectl apply -f`` after substituting the image/namespace placeholders.
 The cluster name, resource group, namespace, context, image and deployment
 name are by-reference knobs; the kubeconfig itself is never emitted.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` consumes
 that PRE-BUILT reference (the immutable ``login_server/image@sha256:...``
 digest produced once by ``package``) as the image, so every environment rolls
 out the identical artifact rather than rebuilding/re-tagging. Absent promotion
 it falls back to the ``$ACR_LOGIN_SERVER``/``$IMAGE_NAME``/``$IMAGE_TAG``
 knobs exactly as before (backward compatible). The Helm path splits the
 reference into ``repository``/``tag`` so a chart's image fields still receive
 the immutable reference.
 """
        cluster = self._knob(ctx, "aks_cluster", _REF_AKS_CLUSTER)
        group = self._knob(ctx, "resource_group", _REF_RESOURCE_GROUP)
        namespace = self._knob(ctx, "namespace", _REF_NAMESPACE)
        context = self._knob(ctx, "kube_context", _REF_KUBE_CONTEXT)
        deployment = self._knob(ctx, "deployment_name", _REF_DEPLOYMENT)

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            # Deploy the pre-built, immutable reference (no rebuild) — The
            # reference is login_server/image@sha256:...; Helm wants repository + tag,
            # so split on '@' and pass the digest as the tag.
            helm_repo = f"$(echo {promo_ref} | cut -d'@' -f1)"
            helm_tag = f"$(echo {promo_ref} | cut -d'@' -f2)"
            image_ref_val = promo_ref
        else:
            login_server = self._knob(ctx, "acr_login_server", _REF_ACR_LOGIN_SERVER)
            image_name = self._knob(ctx, "image_name", _REF_IMAGE_NAME)
            tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
            helm_repo = f"{login_server}/{image_name}"
            helm_tag = tag
            image_ref_val = f"{login_server}/{image_name}:{tag}"

        commands = (
            "echo 'Deploying to AKS (azure-aks)'",
            # Refresh kube credentials for the referenced cluster (keyless via OIDC).
            f"az aks get-credentials --name {cluster} "
            f"--resource-group {group} --overwrite-existing",
            # Prefer a Helm release when a chart exists; else kubectl apply the
            # generated manifests with the image/namespace placeholders filled in.
            "if [ -d chart ] && command -v helm >/dev/null 2>&1; then "
            f"helm upgrade --install {deployment} ./chart "
            f"--namespace {namespace} --create-namespace "
            f"--kube-context {context} "
            f"--set image.repository={helm_repo} --set image.tag={helm_tag}; "
            "else "
            f"export IMAGE_REF={image_ref_val} NAMESPACE={namespace}; "
            f"envsubst < {IAC_DIR}/serviceaccount.yaml "
            f"| kubectl --context {context} -n {namespace} apply -f -; "
            f"envsubst < {IAC_DIR}/deployment.yaml "
            f"| kubectl --context {context} -n {namespace} apply -f -; "
            f"envsubst < {IAC_DIR}/service.yaml "
            f"| kubectl --context {context} -n {namespace} apply -f -; fi",
        )
        return VerbResult(commands=commands)

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Verify the rollout — ``kubectl rollout status``.

 Blocks until the Deployment's rollout completes (all replicas updated and
 available) and surfaces the final pod status for the pipeline log. The
 deployment name, namespace, and context are by-reference knobs.
 """
        namespace = self._knob(ctx, "namespace", _REF_NAMESPACE)
        context = self._knob(ctx, "kube_context", _REF_KUBE_CONTEXT)
        deployment = self._knob(ctx, "deployment_name", _REF_DEPLOYMENT)
        commands = (
            "echo 'Verifying AKS rollout (azure-aks)'",
            f"kubectl --context {context} rollout status "
            f"deployment/{deployment} -n {namespace} --timeout=300s",
            f"kubectl --context {context} get deployment/{deployment} "
            f"-n {namespace} -o wide",
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — AKS refs + ACR + workload identity Terraform + k8s manifests
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the k8s manifests + AKS-ref/ACR/workload-identity Terraform.

 Emits BOTH plain-YAML manifests (a ServiceAccount annotated for Azure Workload
 Identity, a Deployment, and a Service — what the ``kubectl apply`` deploy path
 consumes) AND a Terraform module that references the existing AKS cluster,
 provisions ACR, wires a user-assigned managed identity bound to the
 ServiceAccount through the cluster's OIDC issuer (AKS Workload Identity), and a
 CI identity + OIDC federated credential for keyless CI. Parameterised by the
 IR (project name for naming, ``cloud.region`` for the location default) and by
 Terraform variables / manifest placeholders for every account/environment
 specific — no literal subscription id, tenant, kubeconfig, or secret is written. The ``summary`` feeds the migration runbook.
 """
        project_name = pipeline.project.name
        region = pipeline.cloud.region if pipeline.cloud is not None else None

        app_label = sanitize_name(project_name)
        deployment_name = f"{app_label}-deployment"
        service_name = f"{app_label}-service"
        service_account_name = f"{app_label}-sa"

        files = (
            # Plain-YAML manifests (the kubectl apply deploy path).
            GeneratedFile(
                path=f"{IAC_DIR}/serviceaccount.yaml",
                content=render_serviceaccount_yaml(
                    service_account_name=service_account_name, app_label=app_label
                ),
            ),
            GeneratedFile(
                path=f"{IAC_DIR}/deployment.yaml",
                content=render_deployment_yaml(
                    deployment_name=deployment_name,
                    app_label=app_label,
                    service_account_name=service_account_name,
                ),
            ),
            GeneratedFile(
                path=f"{IAC_DIR}/service.yaml",
                content=render_service_yaml(
                    service_name=service_name, app_label=app_label
                ),
            ),
            # AKS-ref / ACR / workload-identity / federated-credential Terraform.
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
            f"Azure AKS backing infrastructure (Terraform + manifests) for project "
            f"'{project_name}' in {region_text}: plain-YAML manifests (a "
            f"ServiceAccount annotated for Azure Workload Identity, a Deployment, and "
            f"a Service the deploy verb applies with kubectl/helm) AND a Terraform "
            f"module that REFERENCES the existing AKS cluster (it does not create "
            f"one), provisions an Azure Container Registry (ACR) for the built image "
            f"with an AcrPull role assignment for the cluster, a user-assigned managed "
            f"identity bound to the ServiceAccount through the cluster's OIDC issuer "
            f"(AKS Workload Identity), and a user-assigned identity plus an OIDC "
            f"federated credential for keyless CI deploys (no client secret). All "
            f"account specifics (subscription id, tenant, cluster name, OIDC subject) "
            f"are Terraform variables or manifest placeholders — no literal secrets or "
            f"account values are committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
