"""AWS ECS target-platform plugin.

Implements the six stage verbs for the *aws-ecs* platform and supplies the backing
Terraform. The verb meaning is the aws-ecs column:

 | verb | aws-ecs |
 |---------|-----------------------------|
 | lint | lint the Dockerfile | (generic/hadolint, keep reasonable)
 | test | run the project's tests | (from stage.runs, else a placeholder)
 | build | docker build | (+ emits a resilient Dockerfile,)
 | package | push image to ECR |
 | deploy | update ECS service/task def |
 | verify | ECS health check |
 | IaC | ECR, ECS, IAM/OIDC (TF) | (see terraform.py)

Design boundaries this plugin respects
---------------------------------------
* **Secrets by reference only.** Every account specific — the ECR repo URI,
 the AWS region/account, the ECS cluster/service names — is referenced as a shell
 variable (``$ECR_REPO``, ``$AWS_REGION``, …) or a ``settings`` value, never a
 literal. The CI-vendor plugin wires those from ``${{ secrets.X }}`` / variable
 groups; the verbs here just name them. No ARN, key, or account id is emitted.
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / OIDC credential steps are the CI
 vendor's job. A verb never assumes a particular CI vendor.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``ecr_repo`` — the ECR repository URI (else the ``$ECR_REPO`` ref).
 * ``image_tag`` — the image tag (else the ``$IMAGE_TAG`` ref).
 * ``aws_region`` — region (else the IR ``cloud.region``, else ``$AWS_REGION``).
 * ``ecs_cluster`` — ECS cluster name (else the ``$ECS_CLUSTER`` ref).
 * ``ecs_service`` — ECS service name (else the ``$ECS_SERVICE`` ref).
 * ``task_family`` — task-definition family (else the ``$ECS_TASK_FAMILY`` ref).
 * ``container_name`` — container name in the task def (else the ``$ECS_CONTAINER`` ref).

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
IAC_DIR = "iac/aws-ecs"

#: Default by-reference knob names. Secrets/account specifics referenced, not valued
#:. ``settings`` may override the *non-secret* ones (region, names) with values.
_REF_ECR_REPO = "$ECR_REPO"
_REF_IMAGE_TAG = "$IMAGE_TAG"
_REF_AWS_REGION = "$AWS_REGION"
_REF_ECS_CLUSTER = "$ECS_CLUSTER"
_REF_ECS_SERVICE = "$ECS_SERVICE"
_REF_TASK_FAMILY = "$ECS_TASK_FAMILY"
_REF_CONTAINER = "$ECS_CONTAINER"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline. When present, ``package``
#: resolves+exports the pushed image DIGEST into this reference and every
#: ``deploy`` consumes that same reference rather than rebuilding.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


class AwsEcs(TargetPlatformPlugin):
    """Target-platform plugin for AWS ECS (Fargate) over a Docker image.

 ``name`` MUST equal the directory name ``plugins/target/aws-ecs`` so the IR
 value (``target_platform: aws-ecs``), the folder, and this class all agree
 (see base.py module docstring,).
 """

    name = "aws-ecs"

    #: Workload capabilities aws-ecs provides: it builds+pushes a
    #: container image (``container_images``) that can be promoted across
    #: environments as an immutable reference (``artifact_promotion``), deploys per
    #: environment (``environments``), and authenticates to AWS via IAM/OIDC
    #: (``oidc``). All account specifics stay by-reference.
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
 (region, cluster/service names) may be inlined from ``settings``; anything
 not supplied falls back to a shell *reference* so no account specific is
 ever guessed or hard-coded.
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
        """Lint the Dockerfile.

 aws-ecs ships a Dockerfile, so ``lint`` statically checks it. We prefer
 ``hadolint`` when available and fall back to a no-fail notice so the step
 never breaks a pipeline merely because the linter isn't installed — the
 intent (lint the image definition) is expressed either way.
 """
        commands = (
            "echo 'Linting Dockerfile (aws-ecs)'",
            # hadolint if present; otherwise a soft notice (keeps the step portable).
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

 The image is tagged ``<ECR_REPO>:<IMAGE_TAG>`` using by-reference knobs so
 no account specific is baked in. The build also emits a pinned,
 multi-stage Dockerfile with NO live apt-mirror dependency (see
:mod:`.dockerfile`) so the build cannot go red because a distro mirror is
 down — that is the whole point of.
 """
        repo = self._knob(ctx, "ecr_repo", _REF_ECR_REPO)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        commands = (
            "echo 'Building container image (aws-ecs)'",
            f"docker build -t {repo}:{tag} -f {DOCKERFILE_PATH} .",
        )
        dockerfile = GeneratedFile(path=DOCKERFILE_PATH, content=DOCKERFILE_CONTENT)
        return VerbResult(commands=commands, files=(dockerfile,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Push the image to ECR (``package`` = push image to ECR,).

 Authenticates to ECR with an OIDC-derived token (``aws ecr
 get-login-password`` piped into ``docker login``) — no static credentials —
 then pushes the tagged image. The registry host is derived from the ECR
 repo reference and the region reference; both are by-reference.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``package`` resolves
 the pushed image's immutable DIGEST and EXPORTS it into that reference
 variable, so every later ``deploy`` promotes the identical, immutable
 artifact instead of rebuilding. The digest is resolved at runtime from ECR
 (``describe-images``); nothing account- or digest-specific is ever inlined
 here.
 """
        repo = self._knob(ctx, "ecr_repo", _REF_ECR_REPO)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        region = self._region(ctx)
        # The registry host is the repo URI minus the trailing /<name>; derive it in
        # shell so we never need to know the account id here.
        commands = [
            "echo 'Pushing image to ECR (aws-ecs)'",
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
                    "echo 'Resolving immutable artifact reference (aws-ecs promotion)'",
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
        """Register a new task definition revision and roll the ECS service.

 Fetches the current task definition, rewrites its image to the deployable
 reference, registers a new revision, then points the service at it with
 ``aws ecs update-service``. All identifiers are by-reference knobs.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` consumes
 that PRE-BUILT reference (the immutable ``repo@sha256:...`` digest produced
 once by ``package``) rather than rebuilding/re-tagging — so dev, staging and
 prod all roll out the identical artifact. Absent promotion it falls back to
 the ``repo:tag`` knob exactly as before (backward compatible).
 """
        region = self._region(ctx)
        cluster = self._knob(ctx, "ecs_cluster", _REF_ECS_CLUSTER)
        service = self._knob(ctx, "ecs_service", _REF_ECS_SERVICE)
        family = self._knob(ctx, "task_family", _REF_TASK_FAMILY)
        container = self._knob(ctx, "container_name", _REF_CONTAINER)

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            # Deploy the pre-built, immutable reference (no rebuild) —
            new_image_expr = f'NEW_IMAGE="{promo_ref}"'
        else:
            repo = self._knob(ctx, "ecr_repo", _REF_ECR_REPO)
            tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
            new_image_expr = f'NEW_IMAGE="{repo}:{tag}"'

        commands = (
            "echo 'Deploying to ECS (aws-ecs)'",
            # Pull the latest task def JSON and swap in the new image, dropping the
            # read-only fields register-task-definition rejects.
            f"aws ecs describe-task-definition --task-definition {family} "
            f"--region {region} --query taskDefinition > taskdef.json",
            new_image_expr,
            "python -c \"import json,sys,os; d=json.load(open('taskdef.json')); "
            "[c.__setitem__('image', os.environ['NEW_IMAGE']) "
            f"for c in d['containerDefinitions'] if c['name']=='{container}']; "
            "[d.pop(k,None) for k in ('taskDefinitionArn','revision','status',"
            "'requiresAttributes','compatibilities','registeredAt','registeredBy')]; "
            "json.dump(d, open('taskdef-new.json','w'))\"",
            f"NEW_TASK_DEF_ARN=\"$(aws ecs register-task-definition --region {region} "
            "--cli-input-json file://taskdef-new.json "
            "--query taskDefinition.taskDefinitionArn --output text)\"",
            f"aws ecs update-service --cluster {cluster} --service {service} "
            f"--task-definition \"$NEW_TASK_DEF_ARN\" --region {region}",
        )
        return VerbResult(commands=commands)

    def verify(self, ctx: VerbContext) -> VerbResult:
        """ECS health check — wait for the service to reach steady state.

 ``aws ecs wait services-stable`` blocks until the rolled service is healthy
 (running count == desired, deployment complete); a follow-up
 ``describe-services`` prints the status for the pipeline log. All names are
 by-reference knobs.
 """
        region = self._region(ctx)
        cluster = self._knob(ctx, "ecs_cluster", _REF_ECS_CLUSTER)
        service = self._knob(ctx, "ecs_service", _REF_ECS_SERVICE)
        commands = (
            "echo 'Verifying ECS service health (aws-ecs)'",
            f"aws ecs wait services-stable --cluster {cluster} "
            f"--services {service} --region {region}",
            f"aws ecs describe-services --cluster {cluster} --services {service} "
            f"--region {region} "
            "--query 'services[0].{status:status,running:runningCount,desired:desiredCount}'",
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — ECR, ECS, IAM/OIDC Terraform
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning ECR + ECS + IAM/OIDC.

 Parameterised by the IR (project name for naming, ``cloud.region`` for the
 region default) and by Terraform *variables* for every account specific
 (subnets, OIDC subject, image tag). No literal account id, ARN, or secret
 is written. The ``summary`` feeds the migration runbook.
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

        region_text = region if region else "the configured AWS region"
        summary = (
            f"AWS ECS backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: an ECR repository (scan-on-push) "
            f"for the built image, an ECS Fargate cluster + task definition + "
            f"service for the running workload, a GitHub/Azure OIDC identity "
            f"provider plus a scoped IAM deploy role for keyless CI deploys, and "
            f"the ECS task execution role. All account specifics (account id, "
            f"subnets, OIDC subject, image tag) are Terraform variables — no "
            f"literal secrets or account values are committed."
        )
        return IaCArtifacts(files=files, summary=summary)
