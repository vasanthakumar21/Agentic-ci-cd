"""AWS EKS target-platform plugin.

Implements the six stage verbs for the *aws-eks* platform and supplies the backing
Terraform + manifests. The verb meaning is the aws-eks row:

 | verb | aws-eks |
 |---------|-----------------------------------------------|
 | lint | lint the Dockerfile + manifests | (generic/hadolint, keep reasonable)
 | test | run the project's tests | (from stage.runs, else a placeholder)
 | build | docker build | (+ emits a resilient Dockerfile,)
 | package | push image to ECR | (immutable digest = promotable reference)
 | deploy | `kubectl apply` / `helm upgrade` to the cluster| (consumes the pre-built image under promotion)
 | verify | `kubectl rollout status` |
 | IaC | EKS refs + IRSA/OIDC + ECR (Terraform) | (see terraform.py / manifests.py)

EKS is the AWS marriage of the two earlier shapes: it is **container-based like
aws-ecs** (``build`` = ``docker build``, ``package`` = push to ECR, and the immutable
image DIGEST is the promotable reference) but it **deploys like the generic
kubernetes plugin** (``kubectl``/``helm`` apply, ``kubectl rollout status`` to
verify). The plugin is self-contained — it carries its own resilient Dockerfile and
manifests and does not import across plugin boundaries.

Design boundaries this plugin respects
---------------------------------------
* **Secrets by reference only.** Every account/environment specific — the ECR
 repo URI, the AWS region/account, the EKS cluster name, the namespace, the
 kubeconfig context, the deployment name — is referenced as a shell variable
 (``$ECR_REPO``, ``$EKS_CLUSTER``, ``$NAMESPACE``, …) or a ``settings`` value, never
 a literal. No ARN, key, kubeconfig, or account id is emitted. Pod IAM comes from
 IRSA (referenced by the ServiceAccount's role-arn annotation, filled at deploy).
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / OIDC credential / kube-auth steps are the
 CI vendor's job. A verb never assumes a particular CI vendor.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``ecr_repo`` — the ECR repository URI (else the ``$ECR_REPO`` ref).
 * ``image_tag`` — the image tag (else the ``$IMAGE_TAG`` ref).
 * ``aws_region`` — region (else the IR ``cloud.region``, else ``$AWS_REGION``).
 * ``eks_cluster`` — EKS cluster name (else the ``$EKS_CLUSTER`` ref).
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
IAC_DIR = "iac/aws-eks"

#: Default by-reference knob names. Account/environment specifics referenced, not
#: valued. ``settings`` may override the *non-secret* ones (region, names) with
#: values.
_REF_ECR_REPO = "$ECR_REPO"
_REF_IMAGE_TAG = "$IMAGE_TAG"
_REF_AWS_REGION = "$AWS_REGION"
_REF_EKS_CLUSTER = "$EKS_CLUSTER"
_REF_NAMESPACE = "$NAMESPACE"
_REF_DEPLOYMENT = "$DEPLOYMENT_NAME"
_REF_KUBE_CONTEXT = "$KUBE_CONTEXT"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline. When present,
#: ``package`` resolves+exports the pushed image DIGEST into this reference and every
#: ``deploy`` consumes that same reference rather than rebuilding.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


class AwsEks(TargetPlatformPlugin):
    """Target-platform plugin for AWS EKS over a Docker image.

 ``name`` MUST equal the IR value / nested directory home ``plugins/target/aws/
 eks`` resolves to (``aws-eks`` via the ``aws-`` prefix rule) so the IR value, the
 folder, and this class all agree (see base.py module docstring,).
 """

    name = "aws-eks"

    #: Workload capabilities aws-eks provides: it builds+pushes a
    #: container image (``container_images``) that can be promoted across
    #: environments as an immutable digest reference (``artifact_promotion``), rolls
    #: out per environment (``environments``), authenticates to AWS via IAM/OIDC +
    #: IRSA (``oidc``), and references all account specifics by key
    #: (``secrets_by_reference``).
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
 (region, cluster/namespace/deployment names) may be inlined from
 ``settings``; anything not supplied falls back to a shell *reference* so no
 account/environment specific is ever guessed or hard-coded.
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

    # ------------------------------------------------------------------
    # the six stage verbs
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the Dockerfile (aws-eks ships a container image, like aws-ecs).

 aws-eks builds an image, so ``lint`` statically checks the Dockerfile. We
 prefer ``hadolint`` when available and fall back to a no-fail notice so the
 step never breaks a pipeline merely because the linter isn't installed — the
 intent (lint the image definition) is expressed either way.
 """
        commands = (
            "echo 'Linting Dockerfile (aws-eks)'",
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

 EKS runs a container image, so ``build`` is ``docker build`` exactly like
 aws-ecs. The image is tagged ``<ECR_REPO>:<IMAGE_TAG>`` using by-reference
 knobs so no account specific is baked in. The build also emits a pinned,
 multi-stage Dockerfile with NO live apt-mirror dependency (see
:mod:`.dockerfile`) so the build cannot go red because a distro mirror is
 down — that is the whole point of.
 """
        repo = self._knob(ctx, "ecr_repo", _REF_ECR_REPO)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        commands = (
            "echo 'Building container image (aws-eks)'",
            f"docker build -t {repo}:{tag} -f {DOCKERFILE_PATH} .",
        )
        dockerfile = GeneratedFile(path=DOCKERFILE_PATH, content=DOCKERFILE_CONTENT)
        return VerbResult(commands=commands, files=(dockerfile,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Push the image to ECR (``package`` = push image to ECR,).

 Authenticates to ECR with an OIDC-derived token (``aws ecr
 get-login-password`` piped into ``docker login``) — no static credentials —
 then pushes the tagged image. The registry host is derived from the ECR repo
 reference and the region reference; both are by-reference.

 **Promotion.** When the core threads a shared
 artifact reference (``promotion_artifact_ref`` in ``settings``), ``package``
 resolves the pushed image's immutable DIGEST and EXPORTS it into that
 reference variable, so every later ``deploy`` promotes the identical,
 immutable artifact instead of rebuilding. The digest is resolved at runtime
 from ECR (``describe-images``); nothing account- or digest-specific is ever
 inlined here.
 """
        repo = self._knob(ctx, "ecr_repo", _REF_ECR_REPO)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        region = self._region(ctx)
        # The registry host is the repo URI minus the trailing /<name>; derive it in
        # shell so we never need to know the account id here.
        commands = [
            "echo 'Pushing image to ECR (aws-eks)'",
            f"ECR_REGISTRY=\"$(echo {repo} | cut -d'/' -f1)\"",
            f"aws ecr get-login-password --region {region} "
            "| docker login --username AWS --password-stdin \"$ECR_REGISTRY\"",
            f"docker push {repo}:{tag}",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            # Resolve the immutable digest for the just-pushed tag and export it as
            # the shared promotable reference (repo@sha256:...). Every deploy will
            # consume this exact reference — build once, promote the same artifact.
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (aws-eks promotion)'",
                    f"ECR_REPO_NAME=\"$(echo {repo} | cut -d'/' -f2-)\"",
                    f"IMAGE_DIGEST=\"$(aws ecr describe-images --region {region} "
                    "--repository-name \"$ECR_REPO_NAME\" "
                    f"--image-ids imageTag={tag} "
                    "--query 'imageDetails[0].imageDigest' --output text)\"",
                    f"export {var}=\"{repo}@$IMAGE_DIGEST\"",
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Roll the workload out with ``kubectl apply`` / ``helm upgrade``.

 Refreshes cluster credentials with ``aws eks update-kubeconfig`` (keyless, via
 the CI OIDC role), then prefers a Helm release when a chart is present
 (``helm upgrade --install``) and otherwise applies the generated manifests
 with ``kubectl apply -f`` after substituting the image/namespace placeholders.
 The cluster name, namespace, context, image and deployment name are
 by-reference knobs; the kubeconfig itself is never emitted.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` consumes
 that PRE-BUILT reference (the immutable ``repo@sha256:...`` digest produced
 once by ``package``) as the image, so every environment rolls out the
 identical artifact rather than rebuilding/re-tagging. Absent promotion it
 falls back to the ``$ECR_REPO:$IMAGE_TAG`` knobs exactly as before (backward
 compatible). The Helm path splits the reference into ``repository``/``tag`` so
 a chart's image fields still receive the immutable reference.
 """
        region = self._region(ctx)
        cluster = self._knob(ctx, "eks_cluster", _REF_EKS_CLUSTER)
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
            image_ref_val = promo_ref
        else:
            repo = self._knob(ctx, "ecr_repo", _REF_ECR_REPO)
            tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
            helm_repo = repo
            helm_tag = tag
            image_ref_val = f"{repo}:{tag}"

        commands = (
            "echo 'Deploying to EKS (aws-eks)'",
            # Refresh kube credentials for the referenced cluster (keyless via OIDC).
            f"aws eks update-kubeconfig --name {cluster} --region {region}",
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
            "echo 'Verifying EKS rollout (aws-eks)'",
            f"kubectl --context {context} rollout status "
            f"deployment/{deployment} -n {namespace} --timeout=300s",
            f"kubectl --context {context} get deployment/{deployment} "
            f"-n {namespace} -o wide",
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — EKS refs + IRSA/OIDC + ECR Terraform + k8s manifests
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the k8s manifests + EKS-ref/IRSA/OIDC/ECR Terraform.

 Emits BOTH plain-YAML manifests (a ServiceAccount annotated for IRSA, a
 Deployment, and a Service — what the ``kubectl apply`` deploy path consumes)
 AND a Terraform module that references the existing EKS cluster, provisions
 ECR, wires IRSA (the workload IAM role bound to the ServiceAccount via the
 cluster's OIDC provider), and a GitHub/Azure OIDC deploy role for keyless CI.
 Parameterised by the IR (project name for naming, ``cloud.region`` for the
 region default) and by Terraform variables / manifest placeholders for every
 account/environment specific — no literal account id, ARN, kubeconfig, or
 secret is written. The ``summary`` feeds the migration runbook.
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
            # EKS-ref / IRSA / OIDC / ECR Terraform.
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
            f"AWS EKS backing infrastructure (Terraform + manifests) for project "
            f"'{project_name}' in {region_text}: plain-YAML manifests (an IRSA-"
            f"annotated ServiceAccount, a Deployment, and a Service the deploy verb "
            f"applies with kubectl/helm) AND a Terraform module that REFERENCES the "
            f"existing EKS cluster (it does not create one), provisions an ECR "
            f"repository (scan-on-push, immutable tags) for the built image, wires "
            f"IRSA (IAM Roles for Service Accounts — a workload IAM role bound to the "
            f"ServiceAccount through the cluster's OIDC provider), and a "
            f"GitHub/Azure OIDC identity provider plus a scoped IAM deploy role for "
            f"keyless CI deploys. All account specifics (account id, cluster name, "
            f"OIDC subjects, image ref) are Terraform variables or manifest "
            f"placeholders — no literal secrets or account values are committed."
        )
        return IaCArtifacts(files=files, summary=summary)
