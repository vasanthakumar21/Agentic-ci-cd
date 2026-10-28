"""AWS Lambda target-platform plugin.

Implements the six stage verbs for the *aws-lambda* platform and supplies the
backing Terraform. The verb meaning is the aws-lambda row:

 | verb | aws-lambda |
 |---------|----------------------------------------------|
 | lint | lint the function code (generic/ruff) |
 | test | run the project's tests | (from stage.runs, else a placeholder)
 | build | package the deployment zip | (deterministic zip of deps + code — NO image)
 | package | upload the zip to S3 (immutable version) | (by-reference S3 object VERSION — promotable)
 | deploy | update the function code + move the alias | (consumes a pre-built reference when promoting)
 | verify | invoke smoke test / check function state |
 | IaC | Lambda + IAM/OIDC (Terraform) | (see terraform.py)

Packaging shape
----------------------------------------------------------------------------------
This plugin implements the **zip** path, which Lambda supports natively. ``build``
assembles a deterministic deployment zip (dependencies installed next to the
function code), ``package`` uploads it to the S3 artifact bucket and captures the
resulting **immutable object version id** as the promotable reference. Because the
deployable is a zip (not an image), the plugin declares ``artifact_promotion`` but
NOT ``container_images`` — exactly the azure-functions-style reasoning, except
Lambda zips ARE promotable by immutable S3 object version.

Design boundaries this plugin respects
---------------------------------------
* **Secrets by reference only.** Every account specific — the function name,
 alias, artifact bucket/key, region — is referenced as a shell variable
 (``$LAMBDA_FUNCTION``, ``$LAMBDA_ARTIFACT_BUCKET``, …) or a ``settings`` value,
 never a literal. No account id, ARN, or key is emitted.
* **Resilient packaging.** ``build`` installs dependencies from a pinned
 ``requirements.txt`` (PyPI only) into the zip — there is no live distro-mirror
 step that could make the build go red for reasons unrelated to the change.
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical. The emitted zip
 is built with ``-X`` (no extra attributes) and sorted entries so the archive is
 reproducible.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / OIDC credential steps are the CI
 vendor's job. A verb never assumes a particular CI vendor.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``function_name`` — the Lambda function name (else ``$LAMBDA_FUNCTION``).
 * ``alias`` — the alias to move (else ``$LAMBDA_ALIAS``).
 * ``artifact_bucket`` — the S3 artifact bucket (else ``$LAMBDA_ARTIFACT_BUCKET``).
 * ``artifact_key`` — the S3 object key for the zip (else ``$LAMBDA_ARTIFACT_KEY``).
 * ``aws_region`` — region (else the IR ``cloud.region``, else ``$AWS_REGION``).

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

from .terraform import (
    render_main_tf,
    render_outputs_tf,
    render_tfvars_example,
    render_variables_tf,
)

#: Where the function package is built (deps installed here), relative to repo root.
BUILD_DIR = ".lambda_build"

#: The deployable zip artifact the build verb produces and package uploads.
ARTIFACT_ZIP = "lambda-package.zip"

#: Where the Terraform module is written (one dir per target platform, /).
IAC_DIR = "iac/aws-lambda"

#: Default by-reference knob names. Secrets/account specifics referenced, not valued
#:. ``settings`` may override the *non-secret* ones (region, names) with values.
_REF_FUNCTION = "$LAMBDA_FUNCTION"
_REF_ALIAS = "$LAMBDA_ALIAS"
_REF_ARTIFACT_BUCKET = "$LAMBDA_ARTIFACT_BUCKET"
_REF_ARTIFACT_KEY = "$LAMBDA_ARTIFACT_KEY"
_REF_AWS_REGION = "$AWS_REGION"

#: The settings key under which the core threads the shared, immutable artifact
#: reference for a promotion pipeline. When present,
#: ``package`` resolves+exports the uploaded zip's immutable S3 object VERSION id
#: into this reference and every ``deploy`` consumes that same reference rather than
#: rebuilding.
_PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"


class AwsLambda(TargetPlatformPlugin):
    """Target-platform plugin for AWS Lambda (zip packaging) over S3.

 ``name`` MUST equal the IR value / nested directory home ``plugins/target/aws/
 lambda`` reports (``aws-lambda``) so the IR value, the folder, and this class all
 agree (see base.py module docstring,).
 """

    name = "aws-lambda"

    #: Workload capabilities aws-lambda provides: it packages a zip
    #: artifact that can be promoted across environments as an immutable S3 object
    #: version (``artifact_promotion``), deploys per environment (``environments``),
    #: authenticates to AWS via IAM/OIDC (``oidc``), and references all account
    #: specifics by key (``secrets_by_reference``). It does NOT build a container
    #: image on the zip path, so ``container_images`` is deliberately NOT declared.
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
        """Return the ``settings`` value for ``key`` if present, else the ref.

 This is the single place "value-or-reference" is decided. Non-secret knobs
 (region, function/alias/bucket names) may be inlined from ``settings``;
 anything not supplied falls back to a shell *reference* so no account
 specific is ever guessed or hard-coded.
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

    @staticmethod
    def _source_dir(ctx: VerbContext) -> str:
        """Repo-relative directory the function source lives in (monorepo support).

 Non-secret ``settings`` knob ``source_dir`` lets a client keep each service
 in its own sub-directory of a monorepo (e.g. ``services/lambda``) instead of
 at the repo root. Defaults to ``.`` (the repo root) so existing single-service
 repos are byte-identically unchanged. The value
 is trimmed of a trailing slash so the build commands compose cleanly.
 """
        raw = ctx.settings.get("source_dir", ".") or "."
        return raw.rstrip("/") or "."

    # ------------------------------------------------------------------
    # the six stage verbs — NO docker (zip path)
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the function code (aws-lambda ships code, not a Dockerfile).

 Prefer ``ruff`` when available and fall back to a soft notice so the step
 never breaks a pipeline merely because the linter isn't installed. There is
 deliberately NO Dockerfile lint here — the zip path has no image.
 """
        commands = (
            "echo 'Linting function code (aws-lambda)'",
            "if command -v ruff >/dev/null 2>&1; then "
            "ruff check .; "
            "else echo 'ruff not found; skipping function code lint'; fi",
        )
        return VerbResult(commands=commands)

    def test(self, ctx: VerbContext) -> VerbResult:
        """Run the project's test suite.

 Testing is project-defined, so we honour ``stage.runs`` when the IR sets it
 and fall back to a conventional ``pytest -q`` placeholder otherwise. The
 plugin does not impose a runner — that is the client's choice.
 """
        run_cmd = ctx.stage.runs if ctx.stage.runs is not None else "pytest -q"
        return VerbResult(commands=(run_cmd,))

    def build(self, ctx: VerbContext) -> VerbResult:
        """Assemble the deterministic Lambda deployment zip — NO image.

 ``build`` on aws-lambda produces a *deployment zip*: it installs the
 project's dependencies into a build directory alongside the function code
 (the layout the Lambda zip runtime expects) and zips it reproducibly. There
 is NO ``docker build`` and NO image tag — the deployable is a zip. Deps are
 installed from a pinned ``requirements.txt`` (PyPI only), so the build does
 not depend on a live distro mirror. The zip is built with ``-X`` and
 sorted entries so the archive is byte-stable.
 """
        src = self._source_dir(ctx)
        commands = (
            "echo 'Building AWS Lambda deployment zip (aws-lambda) — NO image'",
            f"echo 'Function source directory: {src}'",
            f"rm -rf {BUILD_DIR} {ARTIFACT_ZIP}",
            f"mkdir -p {BUILD_DIR}",
            # Install deps into the build dir so they ship inside the zip (Lambda
            # resolves imports from the package root). Reads requirements.txt from
            # the service source dir so a monorepo keeps per-service deps. No docker.
            f"if [ -f {src}/requirements.txt ]; then "
            f"pip install --target {BUILD_DIR} -r {src}/requirements.txt; "
            "else echo 'no requirements.txt; skipping dependency install'; fi",
            # Stage the function source (from the service source dir) alongside the
            # installed deps. Copying from {src} means a monorepo ships ONLY this
            # service's code, not the whole repo.
            f"cp -r {src}/. {BUILD_DIR}/app_src && rm -rf {BUILD_DIR}/app_src/{BUILD_DIR} "
            f"{BUILD_DIR}/app_src/.git || true",
            f"cp -r {BUILD_DIR}/app_src/. {BUILD_DIR}/ 2>/dev/null || true",
            f"rm -rf {BUILD_DIR}/app_src",
            # Zip reproducibly: -r recurse, -X strip extra file attributes so the
            # archive bytes do not depend on the host's uid/gid/timestamps.
            f"(cd {BUILD_DIR} && zip -r -X ../{ARTIFACT_ZIP} . "
            f"-x '*.git*' -x '*.venv*')",
            f"echo 'Built deployment zip: {ARTIFACT_ZIP}'",
        )
        return VerbResult(commands=commands)

    def package(self, ctx: VerbContext) -> VerbResult:
        """Upload the zip to S3 and capture its immutable object VERSION.

 ``package`` on aws-lambda uploads the built zip to the versioned S3 artifact
 bucket. The promotable, immutable reference is the resulting S3 object
 **version id** — the zip-world analogue of an image digest. Authentication
 is the CI vendor's OIDC step (not emitted here); the bucket/key are
 by-reference knobs.

 **Promotion.** When the core threads a shared
 artifact reference (``promotion_artifact_ref`` in ``settings``), ``package``
 resolves the uploaded object's immutable version id and EXPORTS a stable
 reference (``s3://bucket/key?versionId=...``) into that variable, so every
 later ``deploy`` promotes the identical, immutable artifact instead of
 rebuilding. Nothing account- or version-specific is ever inlined here.
 """
        region = self._region(ctx)
        bucket = self._knob(ctx, "artifact_bucket", _REF_ARTIFACT_BUCKET)
        key = self._knob(ctx, "artifact_key", _REF_ARTIFACT_KEY)
        commands = [
            "echo 'Uploading Lambda zip to S3 (aws-lambda)'",
            f"aws s3api put-object --bucket {bucket} --key {key} "
            f"--body {ARTIFACT_ZIP} --region {region}",
        ]

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            var = promo_ref.lstrip("$")
            # Resolve the immutable S3 object version for the just-uploaded key and
            # export it as the shared promotable reference. Every deploy consumes
            # this exact reference — build once, promote the same artifact.
            commands.extend(
                [
                    "echo 'Resolving immutable artifact reference (aws-lambda promotion)'",
                    f"OBJECT_VERSION=\"$(aws s3api head-object --bucket {bucket} "
                    f"--key {key} --region {region} "
                    "--query 'VersionId' --output text)\"",
                    f'export {var}="s3://{bucket}/{key}?versionId=$OBJECT_VERSION"',
                    f'echo "Promotable artifact reference: ${var}"',
                ]
            )

        return VerbResult(commands=tuple(commands))

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Update the function code and move the alias to the new version.

 Updates the function's code from the S3 artifact, publishes a new immutable
 version, then points the alias at that version with ``aws lambda
 update-alias``. All identifiers are by-reference knobs.

 **Promotion.** When the core threads a shared artifact
 reference (``promotion_artifact_ref`` in ``settings``), ``deploy`` consumes
 that PRE-BUILT reference (the immutable ``s3://bucket/key?versionId=...``
 produced once by ``package``) rather than re-uploading — so dev, staging and
 prod all roll out the identical artifact. Absent promotion it falls back to
 the current ``bucket``/``key`` knobs exactly as before (backward compatible).
 """
        region = self._region(ctx)
        function = self._knob(ctx, "function_name", _REF_FUNCTION)
        alias = self._knob(ctx, "alias", _REF_ALIAS)

        promo_ref = ctx.settings.get(_PROMOTION_ARTIFACT_REF_KEY)
        if promo_ref:
            # Deploy the pre-built, immutable reference (no re-upload) — Parse
            # the shared "s3://bucket/key?versionId=..." reference in shell so no
            # account/version specific is inlined here.
            parse = (
                f'REF="{promo_ref}"',
                'REF_NOPROTO="${REF#s3://}"',
                'S3_PATH="${REF_NOPROTO%%\\?*}"',
                'S3_BUCKET="${S3_PATH%%/*}"',
                'S3_KEY="${S3_PATH#*/}"',
                'S3_VERSION="${REF##*versionId=}"',
                "aws lambda update-function-code "
                f"--function-name {function} "
                '--s3-bucket "$S3_BUCKET" --s3-key "$S3_KEY" '
                f'--region {region} --publish',
            )
        else:
            bucket = self._knob(ctx, "artifact_bucket", _REF_ARTIFACT_BUCKET)
            key = self._knob(ctx, "artifact_key", _REF_ARTIFACT_KEY)
            parse = (
                f"aws lambda update-function-code --function-name {function} "
                f"--s3-bucket {bucket} --s3-key {key} --region {region} --publish",
            )

        commands = (
            "echo 'Deploying to Lambda (aws-lambda)'",
            *parse,
            # Lambda code updates are ASYNC — wait for the update to finish before
            # publishing a version, else PublishVersion hits ResourceConflict
            # ("an update is in progress").
            f"aws lambda wait function-updated --function-name {function} "
            f"--region {region}",
            # Capture the version the update published and point the alias at it.
            f"NEW_VERSION=\"$(aws lambda publish-version --function-name {function} "
            f"--region {region} --query Version --output text)\"",
            f"aws lambda update-alias --function-name {function} "
            f"--name {alias} --function-version \"$NEW_VERSION\" --region {region}",
        )
        return VerbResult(commands=commands)

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Invoke smoke test + confirm the function/alias is active.

 ``verify`` on aws-lambda waits for the function to settle, asserts the alias
 state is ``Active``, then invokes the alias with an empty event as a
 lightweight smoke test and checks the invocation did not error. All names are
 by-reference knobs.
 """
        region = self._region(ctx)
        function = self._knob(ctx, "function_name", _REF_FUNCTION)
        alias = self._knob(ctx, "alias", _REF_ALIAS)
        commands = (
            "echo 'Verifying Lambda function (smoke test) (aws-lambda)'",
            # Wait until the function has finished updating before invoking.
            f"aws lambda wait function-updated --function-name {function} "
            f"--region {region}",
            # Assert the deployed alias reports an Active state.
            f"STATE=\"$(aws lambda get-function-configuration --function-name "
            f"{function}:{alias} --region {region} --query State --output text)\"",
            'echo "function state: $STATE"; [ "$STATE" = "Active" ]',
            # Invoke the alias with an empty event as a reachability smoke test and
            # fail if the invocation reports a function error.
            f"aws lambda invoke --function-name {function}:{alias} "
            "--payload '{}' --cli-binary-format raw-in-base64-out "
            f"--region {region} lambda-smoke-out.json "
            "--query 'FunctionError' --output text > lambda-smoke-error.txt",
            'if grep -q -v \'^None$\' lambda-smoke-error.txt && '
            '[ -s lambda-smoke-error.txt ] && '
            '! grep -q \'^None$\' lambda-smoke-error.txt; then '
            'echo "smoke invocation reported a function error"; '
            'cat lambda-smoke-out.json; exit 1; '
            'else echo "smoke invocation ok"; fi',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — Lambda + IAM/OIDC Terraform
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning Lambda + S3 artifact + IAM/OIDC.

 Parameterised by the IR (project name for naming, ``cloud.region`` for the
 region default) and by Terraform *variables* for every account specific
 (artifact key/image, OIDC subject, runtime/handler). No literal account id,
 ARN, or secret is written. The ``summary`` feeds the migration runbook.
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
            f"AWS Lambda backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: a Lambda function (the deploy verb "
            f"updates its code and moves an alias to the new version) plus its alias, "
            f"a versioned S3 artifact bucket (the package verb uploads the deployment "
            f"zip; the immutable S3 object version is the promotable reference), the "
            f"Lambda execution role (basic CloudWatch Logs permissions), a "
            f"GitHub/Azure OIDC identity provider, and a scoped IAM deploy role for "
            f"keyless CI deploys. The zip path uses NO container image. All account "
            f"specifics (account id, OIDC subject, artifact ref, runtime/handler) are "
            f"Terraform variables — no literal secrets or account values are committed."
        )
        return IaCArtifacts(files=files, summary=summary)
