"""Kubernetes target-platform plugin.

Implements the six stage verbs for the *kubernetes* platform and supplies the
backing IaC. The verb meaning is the kubernetes column:

 | verb | kubernetes |
 |---------|--------------------------------------|
 | lint | lint the Dockerfile + manifests | (generic/hadolint, keep reasonable)
 | test | run the project's tests | (from stage.runs, else a placeholder)
 | build | docker build | (+ emits a resilient Dockerfile,)
 | package | push image to a container registry | (generic registry, NOT ECR-specific)
 | deploy | `kubectl apply` / `helm upgrade` |
 | verify | `kubectl rollout status` |
 | IaC | cluster refs, RBAC (manifests + TF) | (see manifests.py / terraform.py)

Kubernetes is image-based like aws-ecs — ``build`` is ``docker build`` and the
deployable is a container image — but the registry/deploy are generic (NOT ECR /
ECS). The plugin is self-contained: it carries its own resilient Dockerfile (a copy
of the aws-ecs pattern in :mod:`.dockerfile`) and does not import across plugin
boundaries.

Design boundaries this plugin respects
---------------------------------------
* **Secrets by reference only.** Every environment specific — the image repo /
 registry, the namespace, the kubeconfig context, the deployment name — is
 referenced as a shell variable (``$IMAGE_REPO``, ``$NAMESPACE``, …) or a
 ``settings`` value, never a literal. No registry credential, token, or kubeconfig
 content is emitted. Image pulls rely on the cluster's imagePullSecrets (by name).
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / registry-login / kube-auth steps are the
 CI vendor's job. A verb never assumes a particular CI vendor.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``image_repo`` — the image repository (registry/repo) (else ``$IMAGE_REPO``).
 * ``image_tag`` — the image tag (else the ``$IMAGE_TAG`` ref).
 * ``namespace`` — the target namespace (else the ``$NAMESPACE`` ref).
 * ``deployment_name`` — the Deployment name (else the ``$DEPLOYMENT_NAME`` ref).
 * ``kube_context`` — kubeconfig context (else the ``$KUBE_CONTEXT`` ref).
 * ``region`` — region (else the IR ``cloud.region``, else ``$REGION``).

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
IAC_DIR = "iac/kubernetes"

#: Default by-reference knob names. Environment specifics referenced, not valued
#:. ``settings`` may override the *non-secret* ones (names, namespace) with
#: values.
_REF_IMAGE_REPO = "$IMAGE_REPO"
_REF_IMAGE_TAG = "$IMAGE_TAG"
_REF_NAMESPACE = "$NAMESPACE"
_REF_DEPLOYMENT = "$DEPLOYMENT_NAME"
_REF_KUBE_CONTEXT = "$KUBE_CONTEXT"
_REF_REGION = "$REGION"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline. When present, ``package``
#: resolves+exports the pushed image DIGEST into this reference and every
#: ``deploy`` consumes that same reference rather than rebuilding.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


class Kubernetes(TargetPlatformPlugin):
    """Target-platform plugin for Kubernetes over a Docker image.

 ``name`` MUST equal the directory name ``plugins/target/kubernetes`` so the IR
 value (``target_platform: kubernetes``), the folder, and this class all agree
 (see base.py module docstring,).
 """

    name = "kubernetes"

    #: Workload capabilities kubernetes provides: it builds+pushes a
    #: container image (``container_images``) that can be promoted across
    #: environments as an immutable reference (``artifact_promotion``), rolls out
    #: per environment (``environments``), and authenticates via Workload Identity
    #: / OIDC (``oidc``). Environment specifics stay by-reference.
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
 (namespace, names, context) may be inlined from ``settings``; anything not
 supplied falls back to a shell *reference* so no environment specific is
 ever guessed or hard-coded.
 """
        value = ctx.settings.get(key)
        return value if value else ref

    def _region(self, ctx: VerbContext) -> str:
        """Resolve the region: explicit setting > IR cloud.region > ``$REGION``."""
        if ctx.settings.get("region"):
            return ctx.settings["region"]
        cloud = ctx.pipeline.cloud
        if cloud is not None and cloud.region:
            return cloud.region
        return _REF_REGION

    # ------------------------------------------------------------------
    # the six stage verbs
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the Dockerfile (kubernetes ships a container image, like aws-ecs).

 kubernetes builds an image, so ``lint`` statically checks the Dockerfile. We
 prefer ``hadolint`` when available and fall back to a no-fail notice so the
 step never breaks a pipeline merely because the linter isn't installed — the
 intent (lint the image definition) is expressed either way.
 """
        commands = (
            "echo 'Linting Dockerfile (kubernetes)'",
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

 Kubernetes runs a container image, so ``build`` is ``docker build`` exactly
 like aws-ecs. The image is tagged ``<IMAGE_REPO>:<IMAGE_TAG>`` using
 by-reference knobs so no environment specific is baked in. The build
 also emits a pinned, multi-stage Dockerfile with NO live apt-mirror
 dependency (see :mod:`.dockerfile`) so the build cannot go red because a
 distro mirror is down — that is the whole point of.
 """
        repo = self._knob(ctx, "image_repo", _REF_IMAGE_REPO)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        commands = (
            "echo 'Building container image (kubernetes)'",
            f"docker build -t {repo}:{tag} -f {DOCKERFILE_PATH} .",
        )
        dockerfile = GeneratedFile(path=DOCKERFILE_PATH, content=DOCKERFILE_CONTENT)
        return VerbResult(commands=commands, files=(dockerfile,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Push the image to a container registry (``package`` = push to registry,).

 Generic registry push — NOT ECR-specific. Registry login is the CI vendor's
 job (a ``docker login`` driven from the vendor's secret store); this verb
 just pushes the tagged image. The registry/image is the by-reference
 ``$IMAGE_REPO:$IMAGE_TAG`` knob so no registry host or credential is baked in.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``package`` resolves
 the pushed image's immutable DIGEST (``docker inspect`` of the local
 RepoDigest) and EXPORTS it into that reference variable, so every later
 ``deploy`` promotes the identical, immutable artifact instead of rebuilding.
 Nothing registry- or digest-specific is inlined.
 """
        repo = self._knob(ctx, "image_repo", _REF_IMAGE_REPO)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        commands = [
            "echo 'Pushing image to container registry (kubernetes)'",
            f"docker push {repo}:{tag}",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (kubernetes promotion)'",
                    f"IMAGE_DIGEST=\"$(docker inspect --format "
                    f"'{{{{index .RepoDigests 0}}}}' {repo}:{tag})\"",
                    f'export {var}="$IMAGE_DIGEST"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Roll the workload out with ``kubectl apply`` / ``helm upgrade``.

 Prefers a Helm release when a chart is present (``helm upgrade --install``),
 and otherwise applies the generated manifests with ``kubectl apply -f`` after
 substituting the image/namespace placeholders. The namespace, context, image
 and deployment name are by-reference knobs; cluster auth (the kubeconfig
 itself) is the CI vendor's step, never emitted here.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` consumes
 that PRE-BUILT reference (the immutable ``repo@sha256:...`` digest produced
 once by ``package``) as the image, so every environment rolls out the
 identical artifact rather than rebuilding/re-tagging. Absent promotion it
 falls back to the ``$IMAGE_REPO:$IMAGE_TAG`` knobs exactly as before
 (backward compatible). The Helm path splits the digest reference into
 ``repository``/``tag`` (``@`` separator) so a chart's image fields still
 receive the immutable reference.
 """
        namespace = self._knob(ctx, "namespace", _REF_NAMESPACE)
        context = self._knob(ctx, "kube_context", _REF_KUBE_CONTEXT)
        deployment = self._knob(ctx, "deployment_name", _REF_DEPLOYMENT)

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            # Deploy the pre-built, immutable reference (no rebuild) — The
            # reference is repo@sha256:...; Helm wants repository + tag, so split on
            # '@' and pass the digest as the tag.
            helm_repo = f"$(echo {promo_ref} | cut -d'@' -f1)"
            helm_tag = f"$(echo {promo_ref} | cut -d'@' -f2)"
            image_repo_val = promo_ref
            image_tag_val = '""'
        else:
            repo = self._knob(ctx, "image_repo", _REF_IMAGE_REPO)
            tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
            helm_repo = repo
            helm_tag = tag
            image_repo_val = repo
            image_tag_val = tag

        commands = (
            "echo 'Deploying to Kubernetes (kubernetes)'",
            # Prefer a Helm release when a chart exists; else kubectl apply the
            # generated manifests with the image/namespace placeholders filled in.
            "if [ -d chart ] && command -v helm >/dev/null 2>&1; then "
            f"helm upgrade --install {deployment} ./chart "
            f"--namespace {namespace} --create-namespace "
            f"--kube-context {context} "
            f"--set image.repository={helm_repo} --set image.tag={helm_tag}; "
            "else "
            f"export IMAGE_REPO={image_repo_val} IMAGE_TAG={image_tag_val} "
            f"NAMESPACE={namespace}; "
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
            "echo 'Verifying Kubernetes rollout (kubernetes)'",
            f"kubectl --context {context} rollout status "
            f"deployment/{deployment} -n {namespace} --timeout=300s",
            f"kubectl --context {context} get deployment/{deployment} "
            f"-n {namespace} -o wide",
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — k8s manifests + kubernetes-provider Terraform
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the k8s manifests + kubernetes-provider Terraform.

 Emits BOTH a plain-YAML Deployment + Service (what the ``kubectl apply``
 deploy path consumes) AND an equivalent kubernetes-provider Terraform module
 (``kubernetes_deployment`` / ``kubernetes_service``) for clients who manage
 their cluster with Terraform. Parameterised by the IR (project name for
 naming, ``cloud.region`` informational) and by variables/placeholders for the
 image and namespace — no literal registry credential, token, or kubeconfig is
 written. The ``summary`` feeds the migration runbook.
 """
        project_name = pipeline.project.name
        region = pipeline.cloud.region if pipeline.cloud is not None else None

        app_label = sanitize_name(project_name)
        deployment_name = f"{app_label}-deployment"
        service_name = f"{app_label}-service"

        files = (
            # Plain-YAML manifests (the kubectl apply deploy path).
            GeneratedFile(
                path=f"{IAC_DIR}/deployment.yaml",
                content=render_deployment_yaml(
                    deployment_name=deployment_name, app_label=app_label
                ),
            ),
            GeneratedFile(
                path=f"{IAC_DIR}/service.yaml",
                content=render_service_yaml(
                    service_name=service_name, app_label=app_label
                ),
            ),
            # Equivalent kubernetes-provider Terraform (TF-managed clusters).
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

        region_text = region if region else "the configured region"
        summary = (
            f"Kubernetes backing workload for project '{project_name}' "
            f"(cluster in {region_text}): plain-YAML manifests (a Deployment and a "
            f"Service the deploy verb applies with kubectl) AND an equivalent "
            f"kubernetes-provider Terraform module (kubernetes_deployment + "
            f"kubernetes_service) for Terraform-managed clusters. This module does "
            f"NOT create a cluster — it deploys the workload into an existing one "
            f"(design §8b: cluster refs, RBAC). The image reference and namespace "
            f"are variables/placeholders; registry credentials and the kubeconfig "
            f"token are never committed — the provider reads the client's kubeconfig "
            f"and pulls use the cluster's imagePullSecrets by name (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
