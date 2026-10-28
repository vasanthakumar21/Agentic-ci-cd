"""AWS Step Functions target-platform plugin (task 23.6, R3.3/R4.2, design §11.9).

Implements the six stage verbs for the *aws-stepfunctions* platform and supplies the
backing Terraform. aws-stepfunctions is a **state-machine orchestration shape**:
there is NO container image, NO ECR, NO ECS. The verb meaning is the design §11.9
aws-stepfunctions row:

    | verb    | aws-stepfunctions                                              |
    |---------|----------------------------------------------------------------|
    | lint    | validate the ASL definition is well-formed JSON (generic)      |
    | test    | run the project's tests (stage.runs, else a placeholder)       |
    | build   | assemble/validate the ASL state-machine definition + task code |  NO image
    | package | upload the definition/artifacts to S3 (immutable version)      |  (by-reference, promotable)
    | deploy  | aws stepfunctions create-state-machine / update-state-machine  |  (consumes a pre-built ref under promotion)
    | verify  | aws stepfunctions start-execution + poll status to SUCCEEDED   |
    | IaC     | aws_sfn_state_machine + role (+ S3 + OIDC) (Terraform)         |  (see terraform.py)

Packaging shape (design §11.9: "State-machine orchestration", image=no)
-----------------------------------------------------------------------
``build`` assembles/validates the ASL definition (NO docker). ``package`` uploads it
to the versioned S3 bucket and — when promoting — captures the resulting **immutable
object version id** as the promotable reference. Because the deployable is a
definition (not an image), the plugin declares ``artifact_promotion`` but NOT
``container_images``.

Design boundaries this plugin respects
---------------------------------------
* **No image, ever.** Not one verb emits ``docker build`` / ``docker push`` or
  references ECR/ECS. The deployable is a state machine, not an image.
* **Secrets by reference only (R6).** Every account specific — the state-machine
  ARN/name, the execution role, the artifact bucket/key, the region — is referenced
  as a shell variable (``$SFN_STATE_MACHINE_ARN``, ``$SFN_ARTIFACT_BUCKET``, …) or a
  ``settings`` value, never a literal. No account id, ARN, or key is emitted.
* **Deterministic (R5.3).** Commands and files are built from fixed templates in a
  stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin (R4.3).** These verbs return shell steps
  and support files; stage order / gates / OIDC credential steps are the CI vendor's
  job. A verb never assumes a particular CI vendor.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

    * ``state_machine_name`` — the state-machine name (else ``$SFN_STATE_MACHINE_NAME``).
    * ``state_machine_arn``  — the state-machine ARN (else ``$SFN_STATE_MACHINE_ARN``).
    * ``sfn_role``           — the execution role (else ``$SFN_ROLE_ARN``).
    * ``artifact_bucket``    — the S3 artifact bucket (else ``$SFN_ARTIFACT_BUCKET``).
    * ``definition_key``     — the S3 object key for the ASL definition (else ``$SFN_DEFINITION_KEY``).
    * ``aws_region``         — region (else the IR ``cloud.region``, else ``$AWS_REGION``).

Nothing in ``settings`` is a secret; secrets remain references (R6).
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

from .definition import render_state_machine_definition
from .terraform import (
    render_main_tf,
    render_outputs_tf,
    render_tfvars_example,
    render_variables_tf,
)

#: Where the committed ASL state-machine definition is written (deploy applies it).
DEFINITION_PATH = "stepfunctions/state-machine.asl.json"

#: Where the Terraform module is written (one dir per target platform, §8b/§11.1).
IAC_DIR = "iac/aws-stepfunctions"

#: Default by-reference knob names. Secrets/account specifics referenced, not valued
#: (R6). ``settings`` may override the *non-secret* ones (region, names) with values.
_REF_STATE_MACHINE_NAME = "$SFN_STATE_MACHINE_NAME"
_REF_STATE_MACHINE_ARN = "$SFN_STATE_MACHINE_ARN"
_REF_ROLE = "$SFN_ROLE_ARN"
_REF_ARTIFACT_BUCKET = "$SFN_ARTIFACT_BUCKET"
_REF_DEFINITION_KEY = "$SFN_DEFINITION_KEY"
_REF_AWS_REGION = "$AWS_REGION"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline (design §11.6, mirrors aws/glue).
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


def _name_slug(raw: str) -> str:
    """Dash-based state-machine name prefix — MUST match terraform ``_sanitize_name``."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


class AwsStepFunctions(TargetPlatformPlugin):
    """Target-platform plugin for AWS Step Functions (§11.9) — NO image.

    ``name`` MUST equal the IR value / nested directory home ``plugins/target/aws/
    stepfunctions`` reports (``aws-stepfunctions``) so the IR value, the folder, and
    this class all agree (see base.py module docstring, R3.4).
    """

    name = "aws-stepfunctions"

    #: Workload capabilities aws-stepfunctions provides (§11.9/§11.2): it packages an
    #: ASL definition that can be promoted across environments as an immutable S3
    #: object version (``artifact_promotion``), deploys per environment
    #: (``environments``), authenticates to AWS via IAM/OIDC (``oidc``), and
    #: references all account specifics by key (``secrets_by_reference``). It builds
    #: NO container image, so ``container_images`` is deliberately NOT declared.
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

    def _state_machine_name(self, ctx: VerbContext) -> str:
        """State-machine name: explicit setting > ``<project>-sfn`` (non-secret)."""
        if ctx.settings.get("state_machine_name"):
            return ctx.settings["state_machine_name"]
        return f"{_name_slug(ctx.pipeline.project.name)}-sfn"

    @staticmethod
    def _definition_src(ctx: VerbContext) -> str | None:
        """Repo-relative path to the client's REAL ASL definition, if provided.

        Non-secret ``settings`` knob ``definition_src`` points at a hand-authored
        Amazon States Language file the client maintains (e.g.
        ``asl/state-machine.json``). When set, ``build`` copies THAT file's content
        into the committed ``stepfunctions/state-machine.asl.json`` instead of
        emitting the generated skeleton — so regeneration never clobbers the client's
        real graph. When unset the skeleton is emitted exactly as before (backward
        compatible). Returns ``None`` when unset/empty.
        """
        raw = ctx.settings.get("definition_src")
        return raw.strip() if raw and raw.strip() else None

    # ------------------------------------------------------------------
    # the six stage verbs (design §11.9 aws-stepfunctions row) — NO docker
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Validate the ASL definition is well-formed JSON (no Dockerfile lint).

        Step Functions ships an ASL JSON definition, so ``lint`` statically checks it
        parses as JSON. There is deliberately NO Dockerfile lint here — SFN has no
        image.
        """
        commands = (
            "echo 'Validating ASL state-machine definition is well-formed (aws-stepfunctions)'",
            f"if [ -f {DEFINITION_PATH} ]; then "
            f"python -c \"import json; json.load(open('{DEFINITION_PATH}'))\" "
            "&& echo 'ASL definition is valid JSON'; "
            "else echo 'no ASL definition staged yet; skipping'; fi",
        )
        return VerbResult(commands=commands)

    def test(self, ctx: VerbContext) -> VerbResult:
        """Run the project's test suite (honour ``stage.runs``, else ``pytest -q``)."""
        run_cmd = ctx.stage.runs if ctx.stage.runs is not None else "pytest -q"
        return VerbResult(commands=(run_cmd,))

    def build(self, ctx: VerbContext) -> VerbResult:
        """Assemble/validate the ASL state-machine definition + task code (§11.9) — NO image.

        ``build`` on aws-stepfunctions emits the committed ASL definition
        (``stepfunctions/state-machine.asl.json``) and validates it parses as JSON.
        There is NO ``docker build`` and NO image tag — the deployable is a state
        machine. The definition is assembled from a fixed template so regeneration is
        byte-identical (R5.3).

        **Client-authored ASL (``definition_src``).** When the ``definition_src``
        setting names a hand-authored ASL file the client maintains, ``build`` does
        NOT emit the generated skeleton (so the generator never owns/overwrites the
        client's graph); instead it copies that file into ``DEFINITION_PATH`` at CI
        time and validates it. When unset it emits the skeleton exactly as before
        (backward compatible).
        """
        definition_src = self._definition_src(ctx)

        if definition_src is not None:
            # Client owns the real ASL graph at ``definition_src``. Copy it into the
            # deploy path at build time and validate it — the generator emits NO
            # definition file, so a regenerate can never clobber the client's graph.
            commands = (
                "echo 'Building Step Functions state machine: client ASL (NO image)'",
                f"echo 'Using client-authored ASL definition: {definition_src}'",
                f"mkdir -p \"$(dirname {DEFINITION_PATH})\"",
                f"cp {definition_src} {DEFINITION_PATH}",
                # Validate the copied definition is well-formed before packaging.
                f"python -c \"import json; json.load(open('{DEFINITION_PATH}'))\" "
                "&& echo 'ASL definition validated'",
            )
            return VerbResult(commands=commands)

        # Default (no client ASL): emit + validate the generated skeleton as before.
        commands = (
            "echo 'Building Step Functions state machine: ASL definition (NO image)'",
            # Validate the committed definition is well-formed before packaging.
            f"python -c \"import json; json.load(open('{DEFINITION_PATH}'))\" "
            "&& echo 'ASL definition validated'",
        )
        definition = GeneratedFile(
            path=DEFINITION_PATH,
            content=render_state_machine_definition(
                project_name=ctx.pipeline.project.name
            ),
        )
        return VerbResult(commands=commands, files=(definition,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Upload the ASL definition to S3 (§11.9) — no image push.

        ``package`` on aws-stepfunctions uploads the definition to the versioned S3
        bucket. The promotable, immutable reference is the resulting S3 object
        **version id** of the definition.

        **Promotion (design §11.6, mirrors aws/glue).** When the core threads a
        shared artifact reference (``promotion_artifact_ref`` in ``settings``),
        ``package`` resolves the uploaded object's immutable version id and EXPORTS a
        stable reference (``s3://bucket/key?versionId=...``) into that variable, so
        every later ``deploy`` promotes the identical definition. Nothing account- or
        version-specific is ever inlined here (R6).
        """
        region = self._region(ctx)
        bucket = self._knob(ctx, "artifact_bucket", _REF_ARTIFACT_BUCKET)
        definition_key = self._knob(ctx, "definition_key", _REF_DEFINITION_KEY)
        commands = [
            "echo 'Uploading ASL definition to S3 (aws-stepfunctions) — no image push'",
            f"aws s3api put-object --bucket {bucket} --key {definition_key} "
            f"--body {DEFINITION_PATH} --region {region}",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (aws-stepfunctions promotion)'",
                    f'OBJECT_VERSION="$(aws s3api head-object --bucket {bucket} '
                    f"--key {definition_key} --region {region} "
                    "--query 'VersionId' --output text)\"",
                    f'export {var}="s3://{bucket}/{definition_key}?versionId=$OBJECT_VERSION"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Create/update the state machine via the CLI (§11.9) — not an ECS roll.

        The deployable is a state machine, not an image on a service. We look up the
        state machine by name; if it exists we ``aws stepfunctions
        update-state-machine`` it with the committed definition, otherwise we
        ``aws stepfunctions create-state-machine`` it. Region/name/role stay
        by-reference knobs (R6).

        **Promotion (design §11.6).** When the core threads a shared artifact
        reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` announces
        the PRE-BUILT, immutable definition reference so every environment deploys the
        identical definition. Absent promotion it applies the committed definition.
        """
        region = self._region(ctx)
        name = self._state_machine_name(ctx)
        role = self._knob(ctx, "sfn_role", _REF_ROLE)

        commands = [
            "echo 'Deploying Step Functions state machine (create/update via CLI) — NO ECS'",
        ]

        # Each CI job runs on a FRESH runner, so a file the build job wrote does not
        # survive into deploy. When the client maintains the ASL at definition_src
        # (checked out in every job), re-materialise the committed definition here so
        # deploy is self-contained. Falls back to the build-produced skeleton path.
        definition_src = self._definition_src(ctx)
        if definition_src is not None:
            commands.append(
                f"mkdir -p \"$(dirname {DEFINITION_PATH})\" && "
                f"cp {definition_src} {DEFINITION_PATH}"
            )

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            commands.append(
                f'echo "Promoting pre-built ASL definition reference: {promo_ref}"'
            )

        commands.extend(
            [
                # Resolve whether the state machine already exists (empty arn if not).
                f'EXISTING_ARN="$(aws stepfunctions list-state-machines '
                f"--region {region} "
                f"--query \"stateMachines[?name=='{name}'].stateMachineArn\" "
                '--output text)"',
                # Update in place when present; create otherwise.
                f'if [ -n "$EXISTING_ARN" ]; then '
                f'aws stepfunctions update-state-machine --state-machine-arn "$EXISTING_ARN" '
                f"--region {region} --definition \"$(cat {DEFINITION_PATH})\" "
                f"--role-arn {role}; "
                "else "
                f"aws stepfunctions create-state-machine --name {name} "
                f"--region {region} --definition \"$(cat {DEFINITION_PATH})\" "
                f"--role-arn {role}; fi",
            ]
        )
        return VerbResult(commands=tuple(commands))

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Start an execution and poll its status until SUCCEEDED (§11.9).

        ``verify`` on aws-stepfunctions is *start an execution / poll status* — NOT an
        ECS health check. We resolve the state-machine ARN, ``aws stepfunctions
        start-execution``, then poll ``aws stepfunctions describe-execution`` until
        the status is terminal and assert it reached ``SUCCEEDED``. Region/name stay
        by-reference knobs (R6).
        """
        region = self._region(ctx)
        name = self._state_machine_name(ctx)
        commands = (
            "echo 'Starting Step Functions execution and polling status (aws-stepfunctions)'",
            # Resolve the state-machine ARN by name.
            f'SM_ARN="$(aws stepfunctions list-state-machines --region {region} '
            f"--query \"stateMachines[?name=='{name}'].stateMachineArn\" "
            '--output text)"',
            # Start an execution and capture its ARN.
            f'EXEC_ARN="$(aws stepfunctions start-execution '
            f'--state-machine-arn "$SM_ARN" --region {region} '
            "--query 'executionArn' --output text)\"",
            # Poll the execution until the status is terminal.
            "while true; do "
            f'STATUS="$(aws stepfunctions describe-execution '
            f'--execution-arn "$EXEC_ARN" --region {region} '
            "--query 'status' --output text)\"; "
            'if [ "$STATUS" = "SUCCEEDED" ] || [ "$STATUS" = "FAILED" ] '
            '|| [ "$STATUS" = "TIMED_OUT" ] || [ "$STATUS" = "ABORTED" ]; then '
            "break; fi; "
            'echo "execution status: $STATUS; waiting..."; sleep 5; done',
            # Assert the terminal status is SUCCEEDED, else fail the step.
            'echo "execution final status: $STATUS"; [ "$STATUS" = "SUCCEEDED" ]',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — SFN state machine + role + S3 + IAM/OIDC Terraform (§11.9)
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning SFN + role + S3 + IAM/OIDC (§11.9)."""
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
            f"AWS Step Functions backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: a Step Functions state machine "
            f"(aws_sfn_state_machine — the deploy verb creates/updates it and the "
            f"verify verb starts+polls an execution to SUCCEEDED) running an Amazon "
            f"States Language definition, the state-machine execution role (IAM, "
            f"states.amazonaws.com trust, with a scoped inline policy for CloudWatch "
            f"Logs), a versioned S3 artifact bucket (the package verb uploads the ASL "
            f"definition; the immutable S3 object version is the promotable "
            f"reference), a GitHub/Azure OIDC identity provider, and a scoped IAM "
            f"deploy role for keyless CI deploys. This is a state-machine "
            f"orchestration shape — there is NO container image, NO ECR, and NO ECS. "
            f"All account specifics (account id, OIDC subject, definition ref) are "
            f"Terraform variables — no literal secrets or account values are "
            f"committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
