"""Databricks target-platform plugin.

This is the **different-shape proof**. aws-ecs is Docker-shaped: ``build`` =
``docker build``, ``package`` = push to ECR, ``deploy`` = roll an ECS service.
Databricks is the shape MOST unlike that — there is NO container
image, NO ECR, NO ECS anywhere. It proves the six-verb ``TargetPlatformPlugin``
contract is *platform-shaped*, not Docker-shaped:

 | verb | databricks |
 |---------|---------------------------------------------------------|
 | lint | lint the notebooks / python (generic, reasonable) |
 | test | run the project's tests (stage.runs, else pytest) |
 | build | assemble notebooks / build a wheel (python -m build) | NO docker
 | package | upload the artifact to DBFS / a Unity Catalog Volume |
 | deploy | create/update a Databricks JOB via the CLI/API |
 | verify | run the job + poll its run state until TERMINATED/OK |
 | IaC | workspace, Unity Catalog, job (Terraform) | (see terraform.py)

Design boundaries this plugin respects
---------------------------------------
* **No image, ever.** Not one verb emits ``docker build`` / ``docker push`` or
 references ECR/ECS. ``build`` produces a *wheel* (``python -m build``) and stages
 notebooks; the deployable is a Databricks *job*, not an image. That is the whole
 point of this plugin.
* **Secrets by reference only.** The workspace host and token are referenced
 as ``$DATABRICKS_HOST`` / ``$DATABRICKS_TOKEN`` (the CLI's own env contract),
 never as literals. Workspace-specific paths/ids are ``settings`` values or
 ``$REF`` shell variables. No token, workspace URL, or account id is emitted.
* **Deterministic.** Commands and files are built from fixed templates in a
 stable order with no timestamps, so two calls are byte-identical.
* **Orchestration stays in the CI plugin.** These verbs return shell steps
 and support files; stage order / gates / secret wiring is the CI vendor's job.

Settings knobs (all optional, non-secret)
------------------------------------------
``VerbContext.settings`` may carry resolved, non-secret overrides. Recognised keys:

 * ``artifact_path`` — DBFS/Volume path to upload the wheel to (else ``$DATABRICKS_ARTIFACT_PATH``).
 * ``job_name`` — the Databricks job name (else ``<project>-job``).
 * ``job_id`` — an existing job id for run-now/get-run (else ``$DATABRICKS_JOB_ID``).
 * ``package_name`` — installed wheel package name (else derived from project).
 * ``entry_point`` — wheel entry point (else ``main``).

Nothing in ``settings`` is a secret; the host/token remain env references.
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

from .jobspec import render_job_spec
from .terraform import (
    render_main_tf,
    render_outputs_tf,
    render_readme,
    render_tfvars_example,
    render_variables_tf,
)

#: Where the committed Databricks job definition is written (deploy applies it).
JOB_SPEC_PATH = "databricks/job.json"

#: Where the Terraform module is written (one dir per target platform,).
IAC_DIR = "iac/databricks"

#: Build output dir for the staged notebooks + wheel (NO image).
BUILD_DIR = "dist"

#: By-reference knobs. The workspace host + token use the Databricks CLI's own env
#: contract ($DATABRICKS_HOST / $DATABRICKS_TOKEN) so they are NEVER valued.
_REF_HOST = "$DATABRICKS_HOST"
_REF_TOKEN = "$DATABRICKS_TOKEN"
_REF_ARTIFACT_PATH = "$DATABRICKS_ARTIFACT_PATH"
_REF_JOB_ID = "$DATABRICKS_JOB_ID"


def _project_slug(raw: str) -> str:
    """A stable, importable-ish package slug derived from the IR project name.

 Uses underscores (``[a-z0-9_]``) so the result is a valid Python wheel package
 name — used for ``package_name`` and the workspace notebook path.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    return cleaned or "app"


def _job_slug(raw: str) -> str:
    """Dash-based job-name prefix — MUST match terraform ``_sanitize_name`` (9.2).

 The Databricks job name produced by the CLI ``deploy`` verb and by the
 Terraform ``databricks_job`` must agree so the two paths converge on the SAME
 job (consistency,). Both normalise the IR project name to
 ``[a-z0-9-]`` (dashes), so e.g. ``demo-notebooks`` -> ``demo-notebooks-job``
 in both places rather than the underscore package slug.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


class Databricks(TargetPlatformPlugin):
    """Target-platform plugin for Databricks notebooks/jobs — NO image.

 ``name`` MUST equal the directory name ``plugins/target/databricks`` so the IR
 value (``target_platform: databricks``), the folder, and this class all agree
 (see base.py module docstring,).
 """

    name = "databricks"

    #: Workload capabilities databricks provides. The deployable is a
    #: wheel/notebook JOB, NOT a container image — so it declares NEITHER
    #: ``container_images`` NOR ``artifact_promotion`` (the different-shape proof;
    #: promotion wiring for job-shaped targets is out of scope here). It DOES
    #: deploy per environment (``environments``) and authenticates via OIDC
    #: (``oidc``). Host/token stay env references.
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

 Single place "value-or-reference" is decided. Non-secret knobs (paths,
 names) may be inlined from ``settings``; anything not supplied falls back
 to a shell *reference* so nothing workspace-specific is guessed.
 """
        value = ctx.settings.get(key)
        return value if value else ref

    def _job_name(self, ctx: VerbContext) -> str:
        """Job name: explicit setting > ``<project>-job`` (non-secret, derived).

 The derived default uses the dash-based :func:`_job_slug` so it matches the
 Terraform ``databricks_job`` name byte-for-byte — the CLI ``deploy`` verb
 and the Terraform module converge on the SAME job (consistency,).
 """
        if ctx.settings.get("job_name"):
            return ctx.settings["job_name"]
        return f"{_job_slug(ctx.pipeline.project.name)}-job"

    def _package_name(self, ctx: VerbContext) -> str:
        """Wheel package name: explicit setting > derived from the project name."""
        if ctx.settings.get("package_name"):
            return ctx.settings["package_name"]
        return _project_slug(ctx.pipeline.project.name)

    def _entry_point(self, ctx: VerbContext) -> str:
        return self._knob(ctx, "entry_point", "main")

    # ------------------------------------------------------------------
    # the six stage verbs — NO docker anywhere
    # ------------------------------------------------------------------
    def lint(self, ctx: VerbContext) -> VerbResult:
        """Lint the notebooks / python (databricks ships code, not a Dockerfile).

 Prefer ``ruff`` when available and fall back to a soft notice so the step
 never breaks a pipeline merely because the linter isn't installed. There
 is deliberately NO Dockerfile lint here — databricks has no image.
 """
        commands = (
            "echo 'Linting notebooks/python (databricks)'",
            "if command -v ruff >/dev/null 2>&1; then "
            "ruff check .; "
            "else echo 'ruff not found; skipping python lint'; fi",
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
        """Assemble notebooks + build a wheel — NO docker.

 ``build`` on databricks produces a Python *wheel* (``python -m build``)
 into ``dist/`` and stages notebooks alongside it. There is NO ``docker
 build`` and NO image tag — the deployable is a wheel/notebook, not an
 image. Also emits the committed job definition (``databricks/job.json``)
 the ``deploy`` verb applies, referencing the artifact by reference.
 """
        job_name = self._job_name(ctx)
        package_name = self._package_name(ctx)
        entry_point = self._entry_point(ctx)
        artifact_ref = self._knob(ctx, "artifact_path", _REF_ARTIFACT_PATH)

        commands = (
            "echo 'Building Databricks artifact: wheel + notebooks (NO image)'",
            # Build a wheel from the project (PEP 517) — no docker involved.
            "python -m build --wheel --outdir dist/",
            # Stage notebooks for workspace import alongside the wheel.
            f"mkdir -p {BUILD_DIR}/notebooks",
            f"if [ -d notebooks ]; then cp -r notebooks/. {BUILD_DIR}/notebooks/; "
            "else echo 'no notebooks/ dir; wheel-only build'; fi",
        )
        job_spec = GeneratedFile(
            path=JOB_SPEC_PATH,
            content=render_job_spec(
                job_name=job_name,
                package_name=package_name,
                entry_point=entry_point,
                artifact_ref=artifact_ref,
            ),
        )
        return VerbResult(commands=commands, files=(job_spec,))

    def package(self, ctx: VerbContext) -> VerbResult:
        """Upload the artifact to DBFS / a Unity Catalog Volume.

 ``package`` on databricks is NOT an image push — it uploads the built wheel
 (and notebooks) to a workspace path via the ``databricks`` CLI
 (``databricks fs cp`` for DBFS/Volumes, ``databricks workspace import`` for
 notebooks). The destination path and the workspace host/token are
 by-reference: the CLI reads ``$DATABRICKS_HOST`` / ``$DATABRICKS_TOKEN``
 from the environment; the path is a ``settings`` value or ``$REF``.
 """
        artifact_path = self._knob(ctx, "artifact_path", _REF_ARTIFACT_PATH)
        commands = (
            "echo 'Uploading artifact to DBFS/Volumes (databricks) — no image push'",
            # The CLI authenticates from $DATABRICKS_HOST/$DATABRICKS_TOKEN.
            'WHEEL="$(ls dist/*.whl | head -n1)"',
            f'databricks fs cp --overwrite "$WHEEL" "{artifact_path}"',
            # Stage notebooks into the workspace when present (idempotent import).
            f"if [ -d {BUILD_DIR}/notebooks ]; then "
            f"databricks workspace import-dir --overwrite {BUILD_DIR}/notebooks "
            f'"/Shared/{_project_slug(ctx.pipeline.project.name)}"; fi',
        )
        return VerbResult(commands=commands)

    def deploy(self, ctx: VerbContext) -> VerbResult:
        """Create/update a Databricks JOB via the CLI/API — not an ECS roll.

 The deployable is a *job*, not an image on a service. We look up the job by
 name; if it exists we ``databricks jobs reset`` it to the committed spec,
 otherwise we ``databricks jobs create`` it. The spec
 (``databricks/job.json``, emitted by ``build``) references the uploaded
 artifact. Host/token stay env references; the job name is a non-secret
 derived/settings value.
 """
        job_name = self._job_name(ctx)
        commands = (
            "echo 'Deploying Databricks job (create/update via CLI) — NO ECS'",
            # Resolve the job id by name (empty if it does not yet exist).
            f'JOB_ID="$(databricks jobs list --output json '
            f"| python -c \"import json,sys; "
            f"jobs=json.load(sys.stdin).get('jobs', []); "
            f"print(next((str(j['job_id']) for j in jobs "
            f"if j.get('settings', {{}}).get('name')=='{job_name}'), ''))\")\"",
            # Update in place if it exists, else create it from the committed spec.
            f'if [ -n "$JOB_ID" ]; then '
            f'databricks jobs reset --job-id "$JOB_ID" --json @{JOB_SPEC_PATH}; '
            f"else "
            f"databricks jobs create --json @{JOB_SPEC_PATH}; fi",
        )
        return VerbResult(commands=commands)

    def verify(self, ctx: VerbContext) -> VerbResult:
        """Run the job and poll its run state until it finishes.

 ``verify`` on databricks is *run the job / check run state* — NOT an ECS
 health check. We trigger ``databricks jobs run-now``, then poll
 ``databricks jobs get-run`` until the life-cycle state is ``TERMINATED``
 and assert the result state is ``SUCCESS`` (failing the step otherwise).
 The job is addressed by id (settings ``job_id`` or ``$DATABRICKS_JOB_ID``);
 host/token stay env references.
 """
        job_id = self._knob(ctx, "job_id", _REF_JOB_ID)
        commands = (
            "echo 'Running Databricks job and checking run state (databricks)'",
            # Trigger a run and capture its run id.
            f'RUN_ID="$(databricks jobs run-now --job-id {job_id} --output json '
            "| python -c \"import json,sys; print(json.load(sys.stdin)['run_id'])\")\"",
            # Poll the run until the life-cycle state is terminal.
            'while true; do '
            'RUN="$(databricks jobs get-run --run-id "$RUN_ID" --output json)"; '
            'LIFECYCLE="$(echo "$RUN" | python -c "import json,sys; '
            "print(json.load(sys.stdin)['state']['life_cycle_state'])\")\"; "
            'if [ "$LIFECYCLE" = "TERMINATED" ] || [ "$LIFECYCLE" = "SKIPPED" ] '
            '|| [ "$LIFECYCLE" = "INTERNAL_ERROR" ]; then break; fi; '
            'echo "job run state: $LIFECYCLE; waiting..."; sleep 10; done',
            # Assert the terminal result state is SUCCESS, else fail the step.
            'RESULT="$(echo "$RUN" | python -c "import json,sys; '
            "print(json.load(sys.stdin)['state'].get('result_state',''))\")\"; "
            'echo "job result state: $RESULT"; '
            '[ "$RESULT" = "SUCCESS" ]',
        )
        return VerbResult(commands=commands)

    # ------------------------------------------------------------------
    # IaC — workspace, Unity Catalog, job Terraform
    # ------------------------------------------------------------------
    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        """Return the Terraform wiring the Databricks job + optional UC.

 — covers **workspace + job + (optional) Unity Catalog**,
 parameterised by the IR (project name for naming, ``cloud.region`` for an
 informational default) and by Terraform *variables* for every workspace
 specific (host, token, artifact path, catalog, schedule, run-as, cluster
 sizing). Workspace wiring is provider-level host/token *by reference* (the
 token var ``sensitive``); the Unity Catalog schema is OPTIONAL, gated by
 ``var.enable_unity_catalog`` so clients without UC aren't forced into it.
 No literal workspace URL, token, or account id is written.

 Emits a ``databricks_job`` (NOT an ``aws_ecs_*`` resource) — the
 different-shape proof extends to the IaC — plus a ``README.md`` documenting
 the Terraform-vs-CLI split and the honest workspace boundary: creating the
 workspace/metastore is an account-level manual step this module does not own. ``summary`` feeds the runbook.
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
            GeneratedFile(
                path=f"{IAC_DIR}/README.md",
                content=render_readme(project_name),
            ),
        )

        summary = (
            f"Databricks backing infrastructure (Terraform) for project "
            f"'{project_name}': the databricks provider wired to an EXISTING "
            f"workspace by reference (host + sensitive token variables), a "
            f"Databricks job (databricks_job) that runs the uploaded "
            f"wheel/notebook on an ephemeral job cluster — there is NO container "
            f"image, NO ECR, and NO ECS (the shape most unlike aws-ecs) — plus an "
            f"OPTIONAL Unity Catalog schema (gated by var.enable_unity_catalog) the "
            f"job writes into. Creating the workspace/metastore itself is an "
            f"account-level manual step documented in the module README (R7.1). No "
            f"literal workspace URL, token, or account value is committed (R6)."
        )
        return IaCArtifacts(files=files, summary=summary)
