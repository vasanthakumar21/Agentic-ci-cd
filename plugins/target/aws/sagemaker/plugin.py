"""AWS SageMaker target-platform plugin (task 23.6, R3.3/R4.2/R7.1, design §11.9).

Implements the six stage verbs for the *aws-sagemaker* platform and supplies the
backing Terraform. aws-sagemaker is a **container-based ML shape**
(bring-your-own-container) like aws-ecs: ``build`` is ``docker build``, ``package``
pushes to ECR. The verb meaning is the design §11.9 aws-sagemaker row:

    | verb    | aws-sagemaker                                                  |
    |---------|----------------------------------------------------------------|
    | lint    | lint the Dockerfile (generic/hadolint)                         |
    | test    | run the project's tests (stage.runs, else a placeholder)       |
    | build   | docker build training/inference image (+ resilient Dockerfile) |
    | package | push image to ECR (immutable digest as promotable ref)         |
    | deploy  | create/update SageMaker Model + EndpointConfig + Endpoint      |
    | verify  | poll the endpoint status to InService                          |
    | IaC     | SageMaker role + model/endpoint + ECR + S3 + OIDC (TF)        |  (see terraform.py)

Packaging shape (design §11.9: "ML train/deploy", image=yes, promotion=yes)
---------------------------------------------------------------------------
This plugin implements the **container path** (bring-your-own-container). ``build``
runs ``docker build`` and ``package`` pushes the image to ECR, capturing the pushed
image's immutable **digest** as the promotable reference (like aws-ecs). Because the
deployable is an image, the plugin DECLARES ``container_images``.

Design boundaries this plugin respects
---------------------------------------
* **Secrets by reference only (R6).** Every account specific — the ECR repo URI, the
  region, the model/endpoint/endpoint-config names, the execution role, the model
  artifact URI — is referenced as a shell variable (``$ECR_REPO``,
  ``$SAGEMAKER_ENDPOINT``, …) or a ``settings`` value, never a literal. No ARN, key,
  or account id is emitted.
* **Resilient build (R7.1).** ``build`` emits a pinned, multi-stage Dockerfile with
  NO live apt-mirror dependency (see :mod:`.dockerfile`).
* **Deterministic (R5.3).** Commands and files are built from fixed templates in a
  stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin (R4.3).** These verbs return shell steps
  and support files; stage order / gates / OIDC credential steps are the CI vendor's
  job.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

    * ``ecr_repo``          — the ECR repository URI (else ``$ECR_REPO``).
    * ``image_tag``         — the image tag (else ``$IMAGE_TAG``).
    * ``model_name``        — the SageMaker model name (else ``$SAGEMAKER_MODEL``).
    * ``endpoint_name``     — the SageMaker endpoint name (else ``$SAGEMAKER_ENDPOINT``).
    * ``endpoint_config``   — the endpoint-config name (else ``$SAGEMAKER_ENDPOINT_CONFIG``).
    * ``execution_role``    — the SageMaker execution role (else ``$SAGEMAKER_ROLE_ARN``).
    * ``model_data_url``    — the S3 URI of the model artifact (else ``$SAGEMAKER_MODEL_DATA_URL``).
    * ``instance_type``     — the endpoint instance type (else ``$SAGEMAKER_INSTANCE_TYPE``).
    * ``aws_region``        — region (else the IR ``cloud.region``, else ``$AWS_REGION``).

Nothing in ``settings`` is a secret; secrets remain references (R6).
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

#: Where the Terraform module is written (one dir per target platform, §8b/§11.1).
IAC_DIR = "iac/aws-sagemaker"

#: Default by-reference knob names. Secrets/account specifics referenced, not valued
#: (R6). ``settings`` may override the *non-secret* ones (region, names) with values.
_REF_ECR_REPO = "$ECR_REPO"
_REF_IMAGE_TAG = "$IMAGE_TAG"
_REF_MODEL = "$SAGEMAKER_MODEL"
_REF_ENDPOINT = "$SAGEMAKER_ENDPOINT"
_REF_ENDPOINT_CONFIG = "$SAGEMAKER_ENDPOINT_CONFIG"
_REF_ROLE = "$SAGEMAKER_ROLE_ARN"
_REF_MODEL_DATA_URL = "$SAGEMAKER_MODEL_DATA_URL"
_REF_INSTANCE_TYPE = "$SAGEMAKER_INSTANCE_TYPE"
_REF_AWS_REGION = "$AWS_REGION"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline (design §11.6, mirrors aws/ecs). When present,
#: ``package`` resolves+exports the pushed image DIGEST into this reference and every
#: ``deploy`` consumes that same reference rather than rebuilding (R4.2/R6).
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


class AwsSageMaker(TargetPlatformPlugin):
    """Target-platform plugin for AWS SageMaker over a Docker image (§11.9).

    ``name`` MUST equal the IR value / nested directory home ``plugins/target/aws/
    sagemaker`` reports (``aws-sagemaker``) so the IR value, the folder, and this
    class all agree (see base.py module docstring, R3.4).
    """

    name = "aws-sagemaker"

    #: Workload capabilities aws-sagemaker provides (§11.9/§11.2): it builds+pushes a
    #: container image (``container_images``) that can be promoted across
    #: environments as an immutable digest (``artifact_promotion``), deploys per
    #: environment (``environments``), authenticates to AWS via IAM/OIDC (``oidc``),
    #: and references all account specifics by key (``secrets_by_reference``).
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
        """Return the ``settings`` value for ``key`` if present, else the ref."""
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
    # the six stage verbs (design §11.9 aws-sagemaker row) — container shape
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the Dockerfile.

        aws-sagemaker ships a Dockerfile (the bring-your-own-container image), so
        ``lint`` statically checks it. We prefer ``hadolint`` when available and fall
        back to a no-fail notice so the step never breaks merely because the linter
        isn't installed.
        """
        commands = (
            "echo 'Linting Dockerfile (aws-sagemaker)'",
            "if command -v hadolint >/dev/null 2>&1; then "
            f"hadolint {DOCKERFILE_PATH}; "
            "else echo 'hadolint not found; skipping Dockerfile lint'; fi",
        )
        return VerbResult(commands=commands)

    def test(self, ctx: VerbContext) -> VerbResult:
        """Run the project's test suite (honour ``stage.runs``, else ``pytest -q``)."""
        run_cmd = ctx.stage.runs if ctx.stage.runs is not None else "pytest -q"
        return VerbResult(commands=(run_cmd,))

    def build(self, ctx: VerbContext) -> VerbResult:
        """``docker build`` the training/inference image + resilient Dockerfile (R7.1).

        The image is tagged ``<ECR_REPO>:<IMAGE_TAG>`` using by-reference knobs so no
        account specific is baked in (R6). The build also emits a pinned, multi-stage
        Dockerfile with NO live apt-mirror dependency (see :mod:`.dockerfile`) — the
        SageMaker bring-your-own-container inference image.
        """
        repo = self._knob(ctx, "ecr_repo", _REF_ECR_REPO)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        commands = (
            "echo 'Building SageMaker training/inference image (aws-sagemaker)'",
            f"docker build -t {repo}:{tag} -f {DOCKERFILE_PATH} .",
        )
        dockerfile = GeneratedFile(path=DOCKERFILE_PATH, content=DOCKERFILE_CONTENT)
        return VerbResult(commands=commands, files=(dockerfile,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Push the image to ECR (``package`` = push image to ECR, §11.9).

        Authenticates to ECR with an OIDC-derived token (``aws ecr
        get-login-password`` piped into ``docker login``) — no static credentials —
        then pushes the tagged image.

        **Promotion (design §11.6, mirrors aws/ecs).** When the core threads a shared
        artifact reference (``promotion_artifact_ref`` in ``settings``), ``package``
        resolves the pushed image's immutable DIGEST and EXPORTS it into that
        reference variable, so every later ``deploy`` promotes the identical,
        immutable artifact instead of rebuilding. Nothing account- or digest-specific
        is ever inlined here (R6).
        """
        repo = self._knob(ctx, "ecr_repo", _REF_ECR_REPO)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        region = self._region(ctx)
        commands = [
            "echo 'Pushing image to ECR (aws-sagemaker)'",
            f"ECR_REGISTRY=\"$(echo {repo} | cut -d'/' -f1)\"",
            f"aws ecr get-login-password --region {region} "
            "| docker login --username AWS --password-stdin \"$ECR_REGISTRY\"",
            f"docker push {repo}:{tag}",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (aws-sagemaker promotion)'",
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
        """Create/update SageMaker Model + EndpointConfig + Endpoint (§11.9).

        Creates a new versioned Model (pointing at the deployable image + model
        artifact), a new EndpointConfig referencing that model, then creates the
        Endpoint if absent or ``update-endpoint`` with the new config otherwise. All
        identifiers are by-reference knobs (R6).

        **Promotion (design §11.6).** When the core threads a shared artifact
        reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` consumes
        that PRE-BUILT image reference (the immutable ``repo@sha256:...`` digest
        produced once by ``package``) rather than rebuilding/re-tagging. Absent
        promotion it falls back to the ``repo:tag`` knob (backward compatible).
        """
        region = self._region(ctx)
        model = self._knob(ctx, "model_name", _REF_MODEL)
        endpoint = self._knob(ctx, "endpoint_name", _REF_ENDPOINT)
        endpoint_config = self._knob(ctx, "endpoint_config", _REF_ENDPOINT_CONFIG)
        role = self._knob(ctx, "execution_role", _REF_ROLE)
        model_data_url = self._knob(ctx, "model_data_url", _REF_MODEL_DATA_URL)
        instance_type = self._knob(ctx, "instance_type", _REF_INSTANCE_TYPE)

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            new_image_expr = f'NEW_IMAGE="{promo_ref}"'
        else:
            repo = self._knob(ctx, "ecr_repo", _REF_ECR_REPO)
            tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
            new_image_expr = f'NEW_IMAGE="{repo}:{tag}"'

        commands = (
            "echo 'Deploying SageMaker model/endpoint (aws-sagemaker) — NO ECS'",
            new_image_expr,
            # Version the model/config names with the build id so each deploy is
            # immutable (SageMaker models/configs are create-only).
            'DEPLOY_SUFFIX="${GITHUB_SHA:-$(date +%s)}"',
            # Create a new versioned model pointing at the deployable image + artifact.
            f"aws sagemaker create-model --region {region} "
            f'--model-name "{model}-$DEPLOY_SUFFIX" '
            f"--execution-role-arn {role} "
            f'--primary-container "Image=$NEW_IMAGE,ModelDataUrl={model_data_url}"',
            # Create a new endpoint config referencing the new model.
            f"aws sagemaker create-endpoint-config --region {region} "
            f'--endpoint-config-name "{endpoint_config}-$DEPLOY_SUFFIX" '
            f'--production-variants "VariantName=AllTraffic,'
            f'ModelName={model}-$DEPLOY_SUFFIX,InitialInstanceCount=1,'
            f'InstanceType={instance_type}"',
            # Create the endpoint if it does not exist, else update it in place.
            f'if aws sagemaker describe-endpoint --endpoint-name {endpoint} '
            f"--region {region} >/dev/null 2>&1; then "
            f"aws sagemaker update-endpoint --endpoint-name {endpoint} "
            f'--endpoint-config-name "{endpoint_config}-$DEPLOY_SUFFIX" '
            f"--region {region}; "
            "else "
            f"aws sagemaker create-endpoint --endpoint-name {endpoint} "
            f'--endpoint-config-name "{endpoint_config}-$DEPLOY_SUFFIX" '
            f"--region {region}; fi",
        )
        return VerbResult(commands=commands)

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Poll the SageMaker endpoint status until InService (§11.9).

        ``verify`` on aws-sagemaker waits for the endpoint to finish updating, then
        asserts its status is ``InService`` (failing the step otherwise). Region /
        endpoint name stay by-reference knobs (R6).
        """
        region = self._region(ctx)
        endpoint = self._knob(ctx, "endpoint_name", _REF_ENDPOINT)
        commands = (
            "echo 'Polling SageMaker endpoint status to InService (aws-sagemaker)'",
            # Wait until the endpoint has finished deploying/updating.
            f"aws sagemaker wait endpoint-in-service --endpoint-name {endpoint} "
            f"--region {region}",
            # Assert the endpoint reports InService.
            f'STATUS="$(aws sagemaker describe-endpoint --endpoint-name {endpoint} '
            f"--region {region} --query 'EndpointStatus' --output text)\"",
            'echo "endpoint status: $STATUS"; [ "$STATUS" = "InService" ]',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — SageMaker role + model/endpoint + ECR + S3 + OIDC Terraform (§11.9)
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning SageMaker + ECR + S3 + IAM/OIDC (§11.9)."""
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

        region_text = region if region else "the configured AWS region"
        summary = (
            f"AWS SageMaker backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: an ECR repository (scan-on-push, "
            f"immutable tags) for the training/inference image, a versioned S3 bucket "
            f"for model artifacts, a SageMaker model + endpoint configuration + "
            f"endpoint (the deploy verb creates/updates them and the verify verb "
            f"polls the endpoint to InService), the SageMaker execution role (IAM, "
            f"sagemaker.amazonaws.com trust, scoped to pull the image, read model "
            f"artifacts, and write logs), a GitHub/Azure OIDC identity provider, and "
            f"a scoped IAM deploy role for keyless CI deploys. This is a "
            f"container-based ML shape (bring-your-own-container) — it builds a "
            f"Docker image. All account specifics (account id, OIDC subject, image "
            f"tag, model artifact ref) are Terraform variables — no literal secrets "
            f"or account values are committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
