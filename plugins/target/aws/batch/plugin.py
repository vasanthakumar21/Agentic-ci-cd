"""AWS Batch target-platform plugin (task 23.6, R3.3/R4.2/R7.1, design §11.9).

Implements the six stage verbs for the *aws-batch* platform and supplies the backing
Terraform. aws-batch is a **container-based compute shape** like aws-ecs: ``build``
is ``docker build``, ``package`` pushes to ECR. The verb meaning is the design §11.9
aws-batch row:

    | verb    | aws-batch                                                      |
    |---------|----------------------------------------------------------------|
    | lint    | lint the Dockerfile (generic/hadolint)                         |
    | test    | run the project's tests (stage.runs, else a placeholder)       |
    | build   | docker build (+ emits a resilient Dockerfile, R7.1)            |
    | package | push image to ECR (immutable digest as promotable ref)         |
    | deploy  | register/update an AWS Batch job definition pointing at image  |
    | verify  | aws batch submit-job + poll job status to SUCCEEDED            |
    | IaC     | Batch compute env + queue + job def + IAM + ECR + OIDC (TF)    |  (see terraform.py)

Packaging shape (design §11.9: "Batch compute jobs", image=yes, promotion=yes)
------------------------------------------------------------------------------
``build`` runs ``docker build`` and ``package`` pushes the image to ECR, capturing
the pushed image's immutable **digest** as the promotable reference (like aws-ecs).
Because the deployable is an image, the plugin DECLARES ``container_images`` (unlike
the data-job plugins).

Design boundaries this plugin respects
---------------------------------------
* **Secrets by reference only (R6).** Every account specific — the ECR repo URI, the
  region, the job queue / job-definition names, the execution role — is referenced
  as a shell variable (``$ECR_REPO``, ``$BATCH_JOB_QUEUE``, …) or a ``settings``
  value, never a literal. No ARN, key, or account id is emitted.
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

    * ``ecr_repo``            — the ECR repository URI (else ``$ECR_REPO``).
    * ``image_tag``           — the image tag (else ``$IMAGE_TAG``).
    * ``job_definition``      — the Batch job-definition name (else ``$BATCH_JOB_DEFINITION``).
    * ``job_queue``           — the Batch job queue (else ``$BATCH_JOB_QUEUE``).
    * ``execution_role``      — the Batch/ECS execution role (else ``$BATCH_EXECUTION_ROLE``).
    * ``aws_region``          — region (else the IR ``cloud.region``, else ``$AWS_REGION``).

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
from .jobdef import render_job_definition
from .terraform import (
    render_main_tf,
    render_outputs_tf,
    render_tfvars_example,
    render_variables_tf,
)

#: Where the resilient Dockerfile is written, relative to the target repo root.
DOCKERFILE_PATH = "Dockerfile"

#: Where the committed Batch job definition is written (deploy registers it).
JOB_DEF_PATH = "batch/batch-job-definition.json"

#: Where the Terraform module is written (one dir per target platform, §8b/§11.1).
IAC_DIR = "iac/aws-batch"

#: Default by-reference knob names. Secrets/account specifics referenced, not valued
#: (R6). ``settings`` may override the *non-secret* ones (region, names) with values.
_REF_ECR_REPO = "$ECR_REPO"
_REF_IMAGE_TAG = "$IMAGE_TAG"
_REF_JOB_DEFINITION = "$BATCH_JOB_DEFINITION"
_REF_JOB_QUEUE = "$BATCH_JOB_QUEUE"
_REF_EXECUTION_ROLE = "$BATCH_EXECUTION_ROLE"
_REF_AWS_REGION = "$AWS_REGION"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline (design §11.6, mirrors aws/ecs). When present,
#: ``package`` resolves+exports the pushed image DIGEST into this reference and every
#: ``deploy`` consumes that same reference rather than rebuilding (R4.2/R6).
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


class AwsBatch(TargetPlatformPlugin):
    """Target-platform plugin for AWS Batch over a Docker image (§11.9).

    ``name`` MUST equal the IR value / nested directory home ``plugins/target/aws/
    batch`` reports (``aws-batch``) so the IR value, the folder, and this class all
    agree (see base.py module docstring, R3.4).
    """

    name = "aws-batch"

    #: Workload capabilities aws-batch provides (§11.9/§11.2): it builds+pushes a
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
    # the six stage verbs (design §11.9 aws-batch row) — container shape
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the Dockerfile.

        aws-batch ships a Dockerfile, so ``lint`` statically checks it. We prefer
        ``hadolint`` when available and fall back to a no-fail notice so the step
        never breaks a pipeline merely because the linter isn't installed.
        """
        commands = (
            "echo 'Linting Dockerfile (aws-batch)'",
            "if command -v hadolint >/dev/null 2>&1; then "
            f"hadolint {DOCKERFILE_PATH}; "
            "else echo 'hadolint not found; skipping Dockerfile lint'; fi",
        )
        return VerbResult(commands=commands)

    def test(self, ctx: VerbContext) -> VerbResult:
        """Run the project's test suite (honour ``stage.runs``, else ``make test``)."""
        run_cmd = ctx.stage.runs if ctx.stage.runs is not None else "make test"
        return VerbResult(commands=(run_cmd,))

    def build(self, ctx: VerbContext) -> VerbResult:
        """``docker build`` — and emit the resilient Dockerfile (R7.1).

        The image is tagged ``<ECR_REPO>:<IMAGE_TAG>`` using by-reference knobs so no
        account specific is baked in (R6). The build also emits a pinned, multi-stage
        Dockerfile with NO live apt-mirror dependency (see :mod:`.dockerfile`) and the
        committed Batch job definition (``batch/batch-job-definition.json``) the
        ``deploy`` verb registers.
        """
        repo = self._knob(ctx, "ecr_repo", _REF_ECR_REPO)
        tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
        job_def = self._knob(ctx, "job_definition", _REF_JOB_DEFINITION)
        exec_role = self._knob(ctx, "execution_role", _REF_EXECUTION_ROLE)
        commands = (
            "echo 'Building container image (aws-batch)'",
            f"docker build -t {repo}:{tag} -f {DOCKERFILE_PATH} .",
        )
        dockerfile = GeneratedFile(path=DOCKERFILE_PATH, content=DOCKERFILE_CONTENT)
        job_definition = GeneratedFile(
            path=JOB_DEF_PATH,
            content=render_job_definition(
                job_definition_name=job_def,
                image_ref=f"{repo}:{tag}",
                execution_role_ref=exec_role,
            ),
        )
        return VerbResult(commands=commands, files=(dockerfile, job_definition))

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
            "echo 'Pushing image to ECR (aws-batch)'",
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
                    "echo 'Resolving immutable artifact reference (aws-batch promotion)'",
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
        """Register a new Batch job-definition revision pointing at the image (§11.9).

        Rewrites the committed job definition's image to the deployable reference,
        then ``aws batch register-job-definition`` registers a new revision. All
        identifiers are by-reference knobs (R6).

        **Promotion (design §11.6).** When the core threads a shared artifact
        reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` consumes
        that PRE-BUILT reference (the immutable ``repo@sha256:...`` digest produced
        once by ``package``) rather than rebuilding/re-tagging. Absent promotion it
        falls back to the ``repo:tag`` knob exactly as before (backward compatible).
        """
        region = self._region(ctx)

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            new_image_expr = f'NEW_IMAGE="{promo_ref}"'
        else:
            repo = self._knob(ctx, "ecr_repo", _REF_ECR_REPO)
            tag = self._knob(ctx, "image_tag", _REF_IMAGE_TAG)
            new_image_expr = f'NEW_IMAGE="{repo}:{tag}"'

        commands = (
            "echo 'Deploying to AWS Batch (register job definition) — NO ECS service'",
            new_image_expr,
            # Rewrite the committed job definition's image to the deployable
            # reference, dropping read-only fields register-job-definition rejects.
            "python -c \"import json,os; "
            f"d=json.load(open('{JOB_DEF_PATH}')); "
            "d['containerProperties']['image']=os.environ['NEW_IMAGE']; "
            "[d.pop(k,None) for k in ('jobDefinitionArn','revision','status')]; "
            "json.dump(d, open('batch-job-definition-new.json','w'))\"",
            f"aws batch register-job-definition --region {region} "
            "--cli-input-json file://batch-job-definition-new.json",
        )
        return VerbResult(commands=commands)

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Submit a Batch job and poll its status until SUCCEEDED (§11.9).

        ``verify`` on aws-batch is *submit a job / poll job status* — NOT an ECS
        health check. We ``aws batch submit-job`` against the job queue +
        job-definition, then poll ``aws batch describe-jobs`` until the status is
        terminal and assert it reached ``SUCCEEDED``. Region/queue/job-def stay
        by-reference knobs (R6).
        """
        region = self._region(ctx)
        queue = self._knob(ctx, "job_queue", _REF_JOB_QUEUE)
        job_def = self._knob(ctx, "job_definition", _REF_JOB_DEFINITION)
        commands = (
            "echo 'Submitting AWS Batch job and polling status (aws-batch)'",
            # Submit a job and capture its id.
            f'BATCH_JOB_ID="$(aws batch submit-job --job-name {job_def}-verify '
            f"--job-queue {queue} --job-definition {job_def} --region {region} "
            "--query 'jobId' --output text)\"",
            # Poll the job until its status is terminal.
            "while true; do "
            f'STATUS="$(aws batch describe-jobs --jobs "$BATCH_JOB_ID" '
            f"--region {region} --query 'jobs[0].status' --output text)\"; "
            'if [ "$STATUS" = "SUCCEEDED" ] || [ "$STATUS" = "FAILED" ]; then '
            "break; fi; "
            'echo "job status: $STATUS; waiting..."; sleep 10; done',
            # Assert the terminal status is SUCCEEDED, else fail the step.
            'echo "job final status: $STATUS"; [ "$STATUS" = "SUCCEEDED" ]',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — Batch compute env + queue + job def + IAM + ECR + OIDC (§11.9)
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning Batch + ECR + IAM/OIDC (§11.9)."""
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
            f"AWS Batch backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: an ECR repository (scan-on-push, "
            f"immutable tags) for the built image, an AWS Batch Fargate compute "
            f"environment + job queue + job definition (the deploy verb registers "
            f"revisions and the verify verb submits a job + polls to SUCCEEDED), the "
            f"Batch job execution role and Batch service role (IAM), a GitHub/Azure "
            f"OIDC identity provider, and a scoped IAM deploy role for keyless CI "
            f"deploys. This is a container-based compute shape — it builds a Docker "
            f"image. All account specifics (account id, subnets, OIDC subject, image "
            f"tag) are Terraform variables — no literal secrets or account values are "
            f"committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
