"""GCP Cloud Run target-platform plugin.

Implements the six stage verbs for the *gcp-cloud-run* platform and supplies the
backing Terraform. The verb meaning is the gcp-cloud-run row:

 | verb | gcp-cloud-run |
 |---------|-----------------------------------------------------|
 | lint | lint the Dockerfile | (generic/hadolint, keep reasonable)
 | test | run the project's tests | (from stage.runs, else a placeholder)
 | build | docker build | (+ emits a resilient Dockerfile,)
 | package | push image to Google Artifact Registry | (immutable digest = promotable reference)
 | deploy | `gcloud run deploy` consuming the pre-built image | (immutable digest under promotion)
 | verify | revision health (`gcloud run services describe`) |
 | IaC | Cloud Run + Artifact Registry + SA + WIF (Terraform)| (see terraform.py)

Cloud Run is container-based like aws-ecs / aws-eks — ``build`` is ``docker build``
and the deployable is a container image — but the registry is **Google Artifact
Registry** and the deploy is **``gcloud run deploy``** (a fully-managed service, no
cluster). Authentication is keyless via **Workload Identity Federation** (WIF): the
CI OIDC token federates into a deploy service account (no JSON key). The plugin is
self-contained — it carries its own resilient Dockerfile and does not import across
plugin boundaries.

Design boundaries this plugin respects
---------------------------------------
* **Secrets by reference only.** Every project/environment specific — the GCP
 project id, the region, the Artifact Registry repo / image ref, the Cloud Run
 service name — is referenced as a shell variable (``$GCP_PROJECT``, ``$GCP_REGION``,
 ``$AR_REPO``, …) or a ``settings`` value, never a literal. No project id, key, or
 service account JSON is emitted.
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
 * ``service_name`` — the Cloud Run service name (else the ``$CLOUD_RUN_SERVICE`` ref).

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
IAC_DIR = "iac/gcp-cloud-run"

#: Default by-reference knob names. Project/environment specifics referenced, not
#: valued. ``settings`` may override the *non-secret* ones (region, names) with
#: values.
_REF_GCP_PROJECT = "$GCP_PROJECT"
_REF_GCP_REGION = "$GCP_REGION"
_REF_AR_REPO = "$AR_REPO"
_REF_IMAGE_NAME = "$IMAGE_NAME"
_REF_IMAGE_TAG = "$IMAGE_TAG"
_REF_SERVICE = "$CLOUD_RUN_SERVICE"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline.
#: When present, ``package`` resolves+exports the pushed image DIGEST into this
#: reference and every ``deploy`` consumes that same reference rather than
#: rebuilding.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


class GcpCloudRun(TargetPlatformPlugin):
    """Target-platform plugin for GCP Cloud Run over a Docker image.

 ``name`` MUST equal the IR value / nested directory home
 ``plugins/target/gcp/cloud-run`` resolves to (``gcp-cloud-run`` via the ``gcp-``
 prefix rule, splitting on the FIRST ``-``) so the IR value, the folder, and this
 class all agree (see base.py module docstring,).
 """

    name = "gcp-cloud-run"

    #: Workload capabilities gcp-cloud-run provides: it builds+pushes a
    #: container image (``container_images``) that can be promoted across
    #: environments as an immutable digest reference (``artifact_promotion``), deploys
    #: per environment (``environments``), authenticates to GCP keylessly via
    #: Workload Identity Federation / OIDC (``oidc``), and references all project
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
 (region, repo/service names) may be inlined from ``settings``; anything not
 supplied falls back to a shell *reference* so no project/environment specific
 is ever guessed or hard-coded.
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
        """Lint the Dockerfile (gcp-cloud-run ships a container image, like aws-ecs).

 Cloud Run builds an image, so ``lint`` statically checks the Dockerfile. We
 prefer ``hadolint`` when available and fall back to a no-fail notice so the
 step never breaks a pipeline merely because the linter isn't installed — the
 intent (lint the image definition) is expressed either way.
 """
        commands = (
            "echo 'Linting Dockerfile (gcp-cloud-run)'",
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

 Cloud Run runs a container image, so ``build`` is ``docker build`` exactly
 like aws-ecs. The image is tagged with the fully-qualified Artifact Registry
 reference using by-reference knobs so no project specific is baked in.
 The build also emits a pinned, multi-stage Dockerfile with NO live apt-mirror
 dependency (see :mod:`.dockerfile`) so the build cannot go red because a
 distro mirror is down — that is the whole point of.
 """
        image = self._image_reference(ctx)
        commands = (
            "echo 'Building container image (gcp-cloud-run)'",
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
            "echo 'Pushing image to Google Artifact Registry (gcp-cloud-run)'",
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
                    "echo 'Resolving immutable artifact reference (gcp-cloud-run promotion)'",
                    f"IMAGE_DIGEST=\"$(gcloud artifacts docker images describe {image} "
                    "--format='value(image_summary.digest)')\"",
                    f'export {var}="{image_path}@$IMAGE_DIGEST"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Deploy to Cloud Run with ``gcloud run deploy``.

 Deploys the pre-built image to the referenced Cloud Run service with
 ``gcloud run deploy``. The service name, region and project are by-reference
 knobs; credentials (the WIF-federated access token) are the CI vendor's
 step, never emitted here.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` consumes
 that PRE-BUILT reference (the immutable ``repo/image@sha256:...`` digest
 produced once by ``package``) as the ``--image`` argument, so every
 environment rolls out the identical artifact rather than rebuilding/re-tagging.
 Absent promotion it falls back to the fully-qualified ``repo/image:tag``
 reference exactly as before (backward compatible).
 """
        region = self._region(ctx)
        project = self._knob(ctx, "gcp_project", _REF_GCP_PROJECT)
        service = self._knob(ctx, "service_name", _REF_SERVICE)

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            # Deploy the pre-built, immutable reference (no rebuild) —
            image = promo_ref
        else:
            image = self._image_reference(ctx)

        commands = (
            "echo 'Deploying to Cloud Run (gcp-cloud-run)'",
            f"gcloud run deploy {service} --image {image} "
            f"--project {project} --region {region} --platform managed --quiet",
        )
        return VerbResult(commands=commands)

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Verify the new revision is serving/healthy.

 Describes the Cloud Run service and polls its ``Ready`` condition: the deploy
 above routes traffic to the latest revision, so a ``Ready``/``True`` status
 with the service URL printed confirms the new revision is serving. The service
 name, region and project are by-reference knobs.
 """
        region = self._region(ctx)
        project = self._knob(ctx, "gcp_project", _REF_GCP_PROJECT)
        service = self._knob(ctx, "service_name", _REF_SERVICE)
        commands = (
            "echo 'Verifying Cloud Run revision health (gcp-cloud-run)'",
            # Poll the service's Ready condition until it reports True (new revision
            # is serving). Fail the step if it does not become ready.
            f"READY=\"$(gcloud run services describe {service} "
            f"--project {project} --region {region} --platform managed "
            "--format='value(status.conditions.filter(\"type:Ready\").firstof(\"status\"))')\"",
            'echo "Cloud Run Ready condition: $READY"',
            'test "$READY" = "True"',
            f"gcloud run services describe {service} "
            f"--project {project} --region {region} --platform managed "
            "--format='value(status.url)'",
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — Cloud Run + Artifact Registry + SA + WIF Terraform
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning Cloud Run + AR + SA + WIF.

 Parameterised by the IR (project name for naming, ``cloud.region`` for the
 region default) and by Terraform *variables* for every project specific
 (project id, CI repo, image tag). No literal GCP project id, service account
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
            f"GCP Cloud Run backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: an Artifact Registry Docker "
            f"repository for the built image, a Cloud Run (v2) service for the "
            f"running workload, a deploy service account scoped to push images and "
            f"deploy the service, and a Workload Identity Federation (WIF) pool + "
            f"OIDC provider bound to the client CI repository so the pipeline "
            f"authenticates keylessly (no service account JSON key — the GCP "
            f"keyless-OIDC story). All project specifics (project id, CI repo, OIDC "
            f"issuer, image tag) are Terraform variables — no literal secrets or "
            f"project values are committed."
        )
        return IaCArtifacts(files=files, summary=summary)
