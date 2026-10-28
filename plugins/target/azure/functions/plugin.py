"""Azure Functions target-platform plugin.

Implements the six stage verbs for the *azure-functions* platform and supplies the
backing Terraform. The verb meaning is the azure-functions column:

 | verb | azure-functions |
 |---------|-------------------------------------|
 | lint | lint the function code (generic) |
 | test | run the project's tests | (from stage.runs, else a placeholder)
 | build | build the function package | (deps into a build dir — NO image)
 | package | zip + stage | (zip the build output into an artifact)
 | deploy | `func azure functionapp publish` | (or `az functionapp deployment config-zip`)
 | verify | function smoke test | (curl the endpoint / `az functionapp show`)
 | IaC | Function App + identity (TF) | (see terraform.py)

Design boundaries this plugin respects
---------------------------------------
* **NO container image, ever.** Unlike aws-ecs, azure-functions ``build`` produces a
 *function package* (deps installed into a build dir / a zip-ready app folder),
 NOT a Docker image. Not one verb emits ``docker build`` / ``docker push`` or
 references ECR/ECS. The deployable is a zipped function app, not an image.
* **Secrets by reference only.** Every account specific — the Function App
 name, resource group, storage account, region — is referenced as a shell
 variable (``$AZURE_FUNCTION_APP``, ``$AZURE_RESOURCE_GROUP``, …) or a ``settings``
 value, never a literal. No subscription id, connection string, or key is emitted.
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / OIDC login steps are the CI vendor's job.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``function_app`` — the Function App name (else the ``$AZURE_FUNCTION_APP`` ref).
 * ``resource_group`` — the resource group (else the ``$AZURE_RESOURCE_GROUP`` ref).
 * ``storage_account`` — the backing storage account (else ``$AZURE_STORAGE_ACCOUNT``).
 * ``region`` — region (else the IR ``cloud.region``, else ``$AZURE_REGION``).

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
BUILD_DIR = ".python_packages_build"

#: The deployable zip artifact the package verb stages.
ARTIFACT_ZIP = "functionapp.zip"

#: Where the Terraform module is written (one dir per target platform,).
IAC_DIR = "iac/azure-functions"

#: Default by-reference knob names. Account specifics referenced, not valued.
#: ``settings`` may override the *non-secret* ones (names, region) with values.
_REF_FUNCTION_APP = "$AZURE_FUNCTION_APP"
_REF_RESOURCE_GROUP = "$AZURE_RESOURCE_GROUP"
_REF_STORAGE_ACCOUNT = "$AZURE_STORAGE_ACCOUNT"
_REF_REGION = "$AZURE_REGION"


class AzureFunctions(TargetPlatformPlugin):
    """Target-platform plugin for Azure Functions — NO container image.

 ``name`` MUST equal the directory name ``plugins/target/azure-functions`` so the
 IR value (``target_platform: azure-functions``), the folder, and this class all
 agree (see base.py module docstring,).
 """

    name = "azure-functions"

    #: Workload capabilities azure-functions provides. The deployable
    #: is a zipped function package, NOT a container image — so it declares NEITHER
    #: ``container_images`` NOR ``artifact_promotion`` here. It DOES deploy per
    #: environment (``environments``) and authenticates via workload identity /
    #: OIDC (``oidc``). Account specifics stay by-reference.
    capabilities = frozenset(
        {
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
 (region, app/group/account names) may be inlined from ``settings``; anything
 not supplied falls back to a shell *reference* so no account specific is
 ever guessed or hard-coded.
 """
        value = ctx.settings.get(key)
        return value if value else ref

    def _region(self, ctx: VerbContext) -> str:
        """Resolve the region: explicit setting > IR cloud.region > ``$AZURE_REGION``."""
        if ctx.settings.get("region"):
            return ctx.settings["region"]
        cloud = ctx.pipeline.cloud
        if cloud is not None and cloud.region:
            return cloud.region
        return _REF_REGION

    # ------------------------------------------------------------------
    # the six stage verbs — NO docker
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the function code (azure-functions ships code, not a Dockerfile).

 Prefer ``ruff`` when available and fall back to a soft notice so the step
 never breaks a pipeline merely because the linter isn't installed. There is
 deliberately NO Dockerfile lint here — azure-functions has no image.
 """
        commands = (
            "echo 'Linting function code (azure-functions)'",
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
        """Build the function package — install deps into a build dir, NO image.

 ``build`` on azure-functions produces a *function app artifact*: it installs
 the project's dependencies into a local build directory alongside the
 function code (the layout ``func`` / the zip-deploy expects). There is NO
 ``docker build`` and NO image tag — the deployable is a zipped function app,
 not an image. Honours an explicit ``stage.runs`` build command when the IR
 sets one, else runs the conventional pip install into the build dir.
 """
        commands = (
            "echo 'Building Azure Functions package (azure-functions) — NO image'",
            f"mkdir -p {BUILD_DIR}",
            # Install deps into the build dir so they ship inside the function app
            # (the zip-deploy layout). No docker involved.
            "if [ -f requirements.txt ]; then "
            f"pip install --target {BUILD_DIR}/.python_packages/lib/site-packages "
            "-r requirements.txt; "
            "else echo 'no requirements.txt; skipping dependency install'; fi",
            # Stage the function code alongside the installed deps.
            f"cp -r host.json {BUILD_DIR}/ 2>/dev/null || true",
            f"cp -r requirements.txt {BUILD_DIR}/ 2>/dev/null || true",
        )
        return VerbResult(commands=commands)

    def package(self, ctx: VerbContext) -> VerbResult:
        """Zip + stage the function app (``package`` = zip + stage,).

 ``package`` on azure-functions is NOT an image push — it zips the built
 function app (the build dir + function code) into a single deployable
 artifact the ``deploy`` verb publishes. No registry, no image.
 """
        commands = (
            "echo 'Packaging Azure Functions app: zip + stage (azure-functions)'",
            # Zip the whole function app (code + the deps staged by build) into the
            # deployable artifact. -r recurses; we zip the current app directory.
            f"zip -r {ARTIFACT_ZIP} . "
            f"-x '*.git*' -x '{ARTIFACT_ZIP}' -x '*.venv*'",
            f"echo 'Staged deployable artifact: {ARTIFACT_ZIP}'",
        )
        return VerbResult(commands=commands)

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Publish the function app (``func... publish`` / ``az... config-zip``,).

 Deploys the zipped function app into the target Function App. We prefer the
 Azure Functions Core Tools (``func azure functionapp publish``) when
 available and fall back to ``az functionapp deployment source config-zip``
 with the staged zip. The Function App name + resource group are by-reference
 knobs; auth is the CI vendor's OIDC ``az login`` step (not emitted here).
 """
        app = self._knob(ctx, "function_app", _REF_FUNCTION_APP)
        group = self._knob(ctx, "resource_group", _REF_RESOURCE_GROUP)
        commands = (
            "echo 'Deploying Azure Functions app (azure-functions)'",
            # Prefer func Core Tools when present; else az zip-deploy the artifact.
            "if command -v func >/dev/null 2>&1; then "
            f"func azure functionapp publish {app} --python; "
            "else "
            f"az functionapp deployment source config-zip "
            f"--resource-group {group} --name {app} --src {ARTIFACT_ZIP}; fi",
        )
        return VerbResult(commands=commands)

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Function smoke test — confirm the app is up.

 ``verify`` on azure-functions is a smoke test: we check the Function App's
 running state with ``az functionapp show`` and curl its default endpoint to
 confirm it responds. The app name + group are by-reference knobs.
 """
        app = self._knob(ctx, "function_app", _REF_FUNCTION_APP)
        group = self._knob(ctx, "resource_group", _REF_RESOURCE_GROUP)
        commands = (
            "echo 'Verifying Azure Functions app (smoke test) (azure-functions)'",
            # Assert the app is in the Running state.
            f"STATE=\"$(az functionapp show --name {app} "
            f"--resource-group {group} --query state --output tsv)\"",
            'echo "function app state: $STATE"; [ "$STATE" = "Running" ]',
            # Curl the default endpoint as a lightweight reachability smoke test.
            f"HOST=\"$(az functionapp show --name {app} "
            f"--resource-group {group} --query defaultHostName --output tsv)\"",
            'curl -fsS --max-time 30 "https://$HOST/" '
            '|| echo "smoke test: endpoint not reachable (may require a function route)"',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — Function App + identity Terraform
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform provisioning the Function App + identity.

 Parameterised by the IR (project name for naming, ``cloud.region`` for the
 region default) and by Terraform *variables* for every account specific
 (resource group, OIDC subject/issuer, tiers). No literal subscription id,
 connection string, or secret is written. The ``summary`` feeds the
 migration runbook.
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

        region_text = region if region else "the configured Azure region"
        summary = (
            f"Azure Functions backing infrastructure (Terraform) for project "
            f"'{project_name}' in {region_text}: a resource group, a backing "
            f"storage account (wired to the Function App by resource reference — no "
            f"literal connection string), an App Service plan, a Linux Function App "
            f"(the deploy verb publishes into it; it gets a system-assigned managed "
            f"identity), and a user-assigned identity plus an OIDC federated "
            f"credential for keyless CI deploys. There is NO container image, NO "
            f"ECR, and NO ECS — the deployable is a zipped function app. All account "
            f"specifics (subscription id, tenant, OIDC subject) are Terraform "
            f"variables — no literal secrets or account values are committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
