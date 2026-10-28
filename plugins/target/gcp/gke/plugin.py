"""GCP GKE target-platform plugin.

Implements the six stage verbs for the *gcp-gke* platform and supplies the backing
Terraform + manifests. The verb meaning is the gcp-gke row:

 | verb | gcp-gke |
 |---------|-------------------------------------------------------|
 | lint | lint the Dockerfile + manifests | (generic/hadolint, keep reasonable)
 | test | run the project's tests | (from stage.runs, else a placeholder)
 | build | docker build | (+ emits a resilient Dockerfile,)
 | package | push image to Google Artifact Registry | (immutable digest = promotable reference)
 | deploy | `gcloud container clusters get-credentials` + kubectl | (consumes the pre-built image under promotion)
 | verify | `kubectl rollout status` |
 | IaC | GKE refs + Workload Identity + Artifact Registry (TF) | (see terraform.py / manifests.py)

GKE is the GCP marriage of the two earlier shapes: it is **container-based like
gcp-cloud-run** (``build`` = ``docker build``, ``package`` = push to **Google
Artifact Registry**, and the immutable image DIGEST is the promotable reference) but
it **deploys like the generic kubernetes / aws-eks plugins** (``kubectl``/``helm``
apply, ``kubectl rollout status`` to verify). The GKE-specific pieces are: cluster
credentials come from **``gcloud container clusters get-credentials``** (NOT ``aws
eks update-kubeconfig``), the registry is **Artifact Registry** (NOT ECR), and the
workload keyless identity is **GKE Workload Identity** (KSA↔GSA, NOT IRSA). The
plugin is self-contained — it carries its own resilient Dockerfile and manifests and
does not import across plugin boundaries.

Design boundaries this plugin respects
---------------------------------------
* **Secrets by reference only.** Every project/environment specific — the GCP
 project id, the region, the Artifact Registry repo / image ref, the GKE cluster
 name/location, the namespace, the kubeconfig context, the deployment name — is
 referenced as a shell variable (``$GCP_PROJECT``, ``$GCP_REGION``, ``$AR_REPO``,
 ``$GKE_CLUSTER``, ``$NAMESPACE``, …) or a ``settings`` value, never a literal. No
 project id, key, or service account JSON is emitted. Pod identity comes from GKE
 Workload Identity (referenced by the ServiceAccount's GSA annotation).
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
 * ``ar_repo`` — the Artifact Registry repo name (else the ``$AR_REPO`` ref).
 * ``image_name`` — the image name within the repo (else the ``$IMAGE_NAME`` ref).
 * ``image_tag`` — the image tag (else the ``$IMAGE_TAG`` ref).
 * ``gke_cluster`` — the GKE cluster name (else the ``$GKE_CLUSTER`` ref).
 * ``gke_location`` — the GKE cluster location (else the ``$GKE_LOCATION`` ref).
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
IAC_DIR = "iac/gcp-gke"

#: Default by-reference knob names. Project/environment specifics referenced, not
#: valued. ``settings`` may override the *non-secret* ones (region, names) with
#: values.
_REF_GCP_PROJECT = "$GCP_PROJECT"
_REF_GCP_REGION = "$GCP_REGION"
_REF_AR_REPO = "$AR_REPO"
_REF_IMAGE_NAME = "$IMAGE_NAME"
_REF_IMAGE_TAG = "$IMAGE_TAG"
_REF_GKE_CLUSTER = "$GKE_CLUSTER"
_REF_GKE_LOCATION = "$GKE_LOCATION"
_REF_NAMESPACE = "$NAMESPACE"
_REF_DEPLOYMENT = "$DEPLOYMENT_NAME"
_REF_KUBE_CONTEXT = "$KUBE_CONTEXT"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline.
#: When present, ``package`` resolves+exports the pushed image DIGEST into this
#: reference and every ``deploy`` consumes that same reference rather than
#: rebuilding.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


class GcpGke(TargetPlatformPlugin):
    """Target-platform plugin for GCP GKE over a Docker image.

 ``name`` MUST equal the IR value / nested directory home ``plugins/target/gcp/
 gke`` resolves to (``gcp-gke`` via the ``gcp-`` prefix rule) so the IR value, the
 folder, and this class all agree (see base.py module docstring,).
 """

    name = "gcp-gke"

    #: Workload capabilities gcp-gke provides: it builds+pushes a
    #: container image (``container_images``) that can be promoted across
    #: environments as an immutable digest reference (``artifact_promotion``), rolls
    #: out per environment (``environments``), authenticates to GCP keylessly via
    #: Workload Identity Federation / GKE Workload Identity / OIDC (``oidc``), and
    #: references all project specifics by key (``secrets_by_reference``).
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
 (region, repo/cluster/namespace names) may be inlined from ``settings``;
 anything not supplied falls back to a shell *reference* so no
 project/environment specific is ever guessed or hard-coded.
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

    def _image_reference(self, ctx: VerbContext) -> str:
        """Build the fully-qualified Artifact Registry image ref (by-reference, R6).

 Shape: ``<region>-docker.pkg.dev/<project>/<repo>/<image>:<tag>``. Each
 segment is a non-secret knob or a shell reference, so no project id or repo
 literal is baked in unless the client supplied it via ``settings``.
 """
        region = self._region(ctx)
        project = self._knob(ctx, "gcp_project", _REF_GCP_PROJECT)
        repo = self._knob(ctx, "ar_repo", _REF_AR_REPO)
        image = self._knob(ctx, "image_name", _REF_IMAGE_NAME)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        return f"{region}-docker.pkg.dev/{project}/{repo}/{image}:{tag}"

    # ------------------------------------------------------------------
    # the six stage verbs
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the Dockerfile (gcp-gke ships a container image, like aws-ecs).

 GKE builds an image, so ``lint`` statically checks the Dockerfile. We prefer
 ``hadolint`` when available and fall back to a no-fail notice so the step
 never breaks a pipeline merely because the linter isn't installed — the
 intent (lint the image definition) is expressed either way.
 """
        commands = (
            "echo 'Linting Dockerfile (gcp-gke)'",
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

 GKE runs a container image, so ``build`` is ``docker build`` exactly like
 gcp-cloud-run. The image is tagged with the fully-qualified Artifact Registry
 reference using by-reference knobs so no project specific is baked in.
 The build also emits a pinned, multi-stage Dockerfile with NO live apt-mirror
 dependency (see :mod:`.dockerfile`) so the build cannot go red because a
 distro mirror is down — that is the whole point of.
 """
        image = self._image_reference(ctx)
        commands = (
            "echo 'Building container image (gcp-gke)'",
            f"docker build -t {image} -f {DOCKERFILE_PATH} .",
        )
        dockerfile = GeneratedFile(path=DOCKERFILE_PATH, content=DOCKERFILE_CONTENT)
        return VerbResult(commands=commands, files=(dockerfile,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Push the image to Google Artifact Registry (``package`` = push AR,).

 Configures Docker to authenticate to the regional Artifact Registry host with
 the keyless, WIF-federated credentials (``gcloud auth configure-docker``) —
 no static JSON key — then pushes the tagged image. The registry host is
 derived from the region reference; everything is by-reference.

 **Promotion.** When the core
 threads a shared artifact reference (``promotion_artifact_ref`` in
 ``settings``), ``package`` resolves the pushed image's immutable DIGEST
 (``gcloud artifacts docker images describe`` of the tag) and EXPORTS it into
 that reference variable, so every later ``deploy`` promotes the identical,
 immutable artifact (``repo/image@sha256:...``) instead of rebuilding. The
 digest is resolved at runtime; nothing project- or digest-specific is ever
 inlined here.
 """
        region = self._region(ctx)
        image = self._image_reference(ctx)
        commands = [
            "echo 'Pushing image to Google Artifact Registry (gcp-gke)'",
            f"gcloud auth configure-docker {region}-docker.pkg.dev --quiet",
            f"docker push {image}",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            # Resolve the immutable digest for the just-pushed tag and export it as
            # the shared promotable reference (repo/image@sha256:...). Every deploy
            # will consume this exact reference — build once, promote the same image.
            # The image path (without the:tag) is the digest reference's left side.
            image_path = image.rsplit(":", 1)[0]
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (gcp-gke promotion)'",
                    f"IMAGE_DIGEST=\"$(gcloud artifacts docker images describe {image} "
                    "--format='value(image_summary.digest)')\"",
                    f'export {var}="{image_path}@$IMAGE_DIGEST"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Deploy to GKE: get-credentials + ``kubectl apply`` / ``helm upgrade``.

 Fetches cluster credentials with ``gcloud container clusters
 get-credentials`` (keyless, via the CI WIF-federated service account) — the
 GKE-native alternative to ``aws eks update-kubeconfig`` — then prefers a Helm
 release when a chart is present (``helm upgrade --install``) and otherwise
 applies the generated manifests with ``kubectl apply -f`` after substituting
 the image/namespace placeholders. The cluster name/location, namespace,
 context, image and deployment name are by-reference knobs; the
 kubeconfig itself is never emitted.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` consumes
 that PRE-BUILT reference (the immutable ``repo/image@sha256:...`` digest
 produced once by ``package``) as the image, so every environment rolls out
 the identical artifact rather than rebuilding/re-tagging. Absent promotion it
 falls back to the fully-qualified ``repo/image:tag`` reference exactly as
 before (backward compatible). The Helm path splits the reference into
 ``repository``/``tag`` (``@`` separator) so a chart's image fields still
 receive the immutable reference.
 """
        region = self._region(ctx)
        project = self._knob(ctx, "gcp_project", _REF_GCP_PROJECT)
        cluster = self._knob(ctx, "gke_cluster", _REF_GKE_CLUSTER)
        location = self._knob(ctx, "gke_location", _REF_GKE_LOCATION)
        namespace = self._knob(ctx, "namespace", _REF_NAMESPACE)
        context = self._knob(ctx, "kube_context", _REF_KUBE_CONTEXT)
        deployment = self._knob(ctx, "deployment_name", _REF_DEPLOYMENT)

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            # Deploy the pre-built, immutable reference (no rebuild) — The
            # reference is repo/image@sha256:...; Helm wants repository + tag, so
            # split on '@' and pass the digest as the tag.
            helm_repo = f"$(echo {promo_ref} | cut -d'@' -f1)"
            helm_tag = f"$(echo {promo_ref} | cut -d'@' -f2)"
            image_ref_val = promo_ref
        else:
            image_ref_val = self._image_reference(ctx)
            helm_repo = image_ref_val.rsplit(":", 1)[0]
            helm_tag = image_ref_val.rsplit(":", 1)[1] if ":" in image_ref_val else "latest"

        commands = (
            "echo 'Deploying to GKE (gcp-gke)'",
            # Fetch cluster credentials for the referenced GKE cluster (keyless via
            # the WIF-federated deploy service account). The GKE-native get-credentials
            # — NOT aws eks update-kubeconfig.
            f"gcloud container clusters get-credentials {cluster} "
            f"--location {location} --project {project}",
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
            "echo 'Verifying GKE rollout (gcp-gke)'",
            f"kubectl --context {context} rollout status "
            f"deployment/{deployment} -n {namespace} --timeout=300s",
            f"kubectl --context {context} get deployment/{deployment} "
            f"-n {namespace} -o wide",
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — GKE refs + Workload Identity + Artifact Registry + WIF
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the k8s manifests + GKE-ref/Workload-Identity/AR/WIF Terraform.

 Emits BOTH plain-YAML manifests (a ServiceAccount annotated for GKE Workload
 Identity, a Deployment, and a Service — what the ``kubectl apply`` deploy path
 consumes) AND a Terraform module that REFERENCES the existing GKE cluster
 (data source), provisions an Artifact Registry Docker repository, a deploy
 service account, a runtime Google service account + the GKE Workload Identity
 binding (KSA impersonates GSA), and a Workload Identity Federation pool +
 OIDC provider for keyless CI. Parameterised by the IR (project name for
 naming, ``cloud.region`` for the region default) and by Terraform variables /
 manifest placeholders for every project/environment specific — no literal
 project id, key, kubeconfig, or secret is written. The ``summary`` feeds
 the migration runbook.
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
            # GKE-ref / Workload Identity / Artifact Registry / WIF Terraform.
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
            f"GCP GKE backing infrastructure (Terraform + manifests) for project "
            f"'{project_name}' in {region_text}: plain-YAML manifests (a Workload-"
            f"Identity-annotated ServiceAccount, a Deployment, and a Service the "
            f"deploy verb applies with kubectl/helm) AND a Terraform module that "
            f"REFERENCES the existing GKE cluster (it does not create one), "
            f"provisions an Artifact Registry Docker repository for the built image, "
            f"a deploy service account scoped to push images and run kubectl/helm, a "
            f"runtime Google service account plus the GKE Workload Identity binding "
            f"(the Kubernetes ServiceAccount impersonates the Google service account "
            f"via the cluster's Workload Identity pool — the GCP analogue of EKS "
            f"IRSA), and a Workload Identity Federation (WIF) pool + OIDC provider "
            f"bound to the client CI repository so the pipeline authenticates "
            f"keylessly (no service account JSON key). All project specifics (project "
            f"id, cluster name, CI repo, OIDC issuer) are Terraform variables or "
            f"manifest placeholders — no literal secrets or project values are "
            f"committed."
        )
        return IaCArtifacts(files=files, summary=summary)
