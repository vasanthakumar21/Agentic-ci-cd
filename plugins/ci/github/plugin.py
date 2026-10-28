"""GitHub Actions workflow emitter.

Renders the vendor-neutral :class:`StageGraph` into a single GitHub Actions
workflow file, ``.github/workflows/ci.yml``. The plugin is pure *orchestration*: it arranges stage order, manual gates, approvals, timeouts, security
scans and cloud OIDC — it never decides what a stage *does*. The command a stage
runs is taken verbatim from ``stage.runs`` (the IR), because the target-platform
verbs (what build/deploy actually do) are composed elsewhere at dispatch time;
``emit_pipeline_files`` only receives the stage graph.

 — GitHub Actions column mapping
------------------------------------------
 * stage order -> ``jobs`` + ``needs`` (each job ``needs`` the previous
 *enabled* stage's job, so the declared order is enforced as a linear chain).
 * manual gate -> a job that is ``when=manual`` OR targets an environment
 whose ``gate=manual`` gets the GitHub ``environment:`` key; the required
 reviewers / protection are configured at the GitHub environment level by the
 bootstrap step, the workflow only *references* ``environment: <name>``.
 * approvals -> same ``environment:`` reference (env reviewers enforce it).
 * timeout -> ``timeout-minutes`` on the job (``stage.timeout_minutes``).
 * secrets ref -> ``${{ secrets.NAME }}`` — by reference only, never values. Role ARNs / subscription ids come from secret references too.
 * cloud OIDC -> when ``cloud.auth == oidc`` the pipeline requests
 ``permissions: id-token: write`` and a provider-appropriate credentials step
 (aws -> ``aws-actions/configure-aws-credentials``; azure -> ``azure/login``;
 gcp -> ``google-github-actions/auth``).
 * scans -> any stage requesting ``scans`` contributes steps to a single
 ``security`` job (SAST / secret / dependency scan steps, kept generic).

Determinism
------------------
The workflow must regenerate byte-for-byte. We therefore emit YAML with a small,
explicit, ordered serializer (:mod:`.yaml_emit`) instead of a general YAML dumper
whose key order / style can drift. There are no timestamps, no ``hash``-ordered
dicts, and every collection is built in a fixed, documented order.

``retries`` (honest limitation)
--------------------------------
GitHub Actions has no native *job* retry. When a stage sets ``retries > 0`` we add
a short comment on the job noting the limitation (a step-level retry action could
be wired in later). We do not silently drop the intent.
"""

from __future__ import annotations

import re

from cicdgen.ir.models import Cloud, CloudAuth, CloudProvider, GateMode, Pipeline, Stage, WhenMode
from cicdgen.plugins.base import Capability, CIVendorPlugin, GeneratedFile, StageGraph

from .yaml_emit import Comment, YamlValue, dump_mapping

#: Where the workflow is written, relative to the target repo root.
WORKFLOW_PATH = ".github/workflows/ci.yml"

#: The runner image used for every job. A fixed, documented default keeps output
#: deterministic; a future IR knob could override it.
DEFAULT_RUNNER = "ubuntu-latest"

#: Pinned action references (major-version tags). Pinning keeps the emitted
#: workflow deterministic and is the recommended supply-chain hygiene.
ACTION_CHECKOUT = "actions/checkout@v4"
ACTION_AWS_OIDC = "aws-actions/configure-aws-credentials@v4"
ACTION_AZURE_LOGIN = "azure/login@v2"
ACTION_GCP_AUTH = "google-github-actions/auth@v2"

#: Generic scan step actions (kept vendor-neutral / placeholder-ish). The intent
#: is a correct *structure*; the exact scanner is a detail the agent can gap-fill.
SCAN_STEP_ACTIONS: dict[str, tuple[str, str]] = {
    # scan type -> (step name, run command)
    "sast": ("SAST scan", "echo 'run SAST scan'"),
    "secrets": ("Secret scan", "echo 'run secret scan'"),
    "dependency": ("Dependency scan", "echo 'run dependency scan'"),
}

#: Secret key names referenced for cloud OIDC credentials (by reference,).
AWS_ROLE_SECRET = "AWS_DEPLOY_ROLE_ARN"
#: Secret key names for static-key AWS auth (used when cloud.auth == keys).
AWS_ACCESS_KEY_ID_SECRET = "AWS_ACCESS_KEY_ID"
AWS_SECRET_ACCESS_KEY_SECRET = "AWS_SECRET_ACCESS_KEY"
AZURE_CLIENT_ID_SECRET = "AZURE_CLIENT_ID"
AZURE_TENANT_ID_SECRET = "AZURE_TENANT_ID"
AZURE_SUBSCRIPTION_ID_SECRET = "AZURE_SUBSCRIPTION_ID"
GCP_WIF_PROVIDER_SECRET = "GCP_WORKLOAD_IDENTITY_PROVIDER"
GCP_SERVICE_ACCOUNT_SECRET = "GCP_SERVICE_ACCOUNT"


def _job_id(stage_id: str) -> str:
    """Map a stage id to a GitHub Actions job id (job ids allow ``-`` and ``_``).

 Stage ids are user-defined; GitHub job ids must match ``[A-Za-z_][A-Za-z0-9_-]*``.
 We lower-case and replace any disallowed char with ``-`` deterministically, and
 prefix a ``j_`` when the id does not start with a letter/underscore.
 """
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in stage_id).lower()
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"j_{cleaned}"
    return cleaned


class GitHubActions(CIVendorPlugin):
    """Emit a GitHub Actions workflow from a stage graph.

 ``name`` MUST equal the directory name ``plugins/ci/github`` so the IR value,
 the folder, and this class all agree (see base.py module docstring,).
 """

    name = "github"

    #: Pipeline-expression capabilities GitHub Actions can render:
    #: env reviewers (approvals), manual-gated environments (manual gates), cloud
    #: OIDC, self-hosted runners, matrix jobs, and the security scan job. Secret
    #: references (``${{ secrets.X }}``) are emitted by value-free reference.
    capabilities = frozenset(
        {
            Capability.APPROVALS,
            Capability.MANUAL_GATES,
            Capability.OIDC,
            Capability.SELF_HOSTED_RUNNERS,
            Capability.MATRIX_BUILDS,
            Capability.SECURITY_SCANS,
            Capability.SECRETS_BY_REFERENCE,
        }
    )

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        """Render ``.github/workflows/ci.yml`` for ``graph``.

 Builds one job per *enabled* stage (preserving declared order via
 ``needs``), an optional ``security`` job when any stage requests scans, and
 wires cloud OIDC permissions/steps when ``cloud.auth == oidc``.
 """
        pipeline = graph.pipeline
        enabled = [s for s in graph.stages if s.enabled]
        env_gate = {e.name: e.gate for e in pipeline.environments}

        workflow = self._build_workflow(graph, pipeline, enabled, env_gate)
        content = dump_mapping(workflow)
        return (GeneratedFile(path=WORKFLOW_PATH, content=content),)

    # ------------------------------------------------------------------
    # Workflow assembly (ordered, deterministic)
    # ------------------------------------------------------------------
    def _build_workflow(
        self,
        graph: StageGraph,
        pipeline: Pipeline,
        enabled: list[Stage],
        env_gate: dict[str, GateMode],
    ) -> dict[str, YamlValue]:
        """Assemble the whole workflow mapping in a fixed key order."""
        workflow: dict[str, YamlValue] = {}
        workflow["name"] = f"{pipeline.project.name} CI"
        # Triggers: the IR's ``triggers`` block drives which branches start the
        # pipeline (e.g. TekFlow branch-per-ticket: push on main + feature/** +
        # TEK-*). Absent => the documented default of main-only (backward
        # compatible). Lists render as explicit branch patterns the vendor globs.
        push_branches = ["main"]
        pr_branches = ["main"]
        if pipeline.triggers is not None:
            push_branches = list(pipeline.triggers.push_branches)
            pr_branches = list(pipeline.triggers.pr_branches)
        workflow["on"] = {
            "push": {"branches": push_branches},
            "pull_request": {"branches": pr_branches},
            "workflow_dispatch": {},
        }

        # A cloud login step is needed for OIDC OR static keys; only OIDC needs the
        # id-token permission (static keys use secrets, not a minted token).
        needs_cloud_auth = self._uses_cloud_auth(pipeline.cloud)
        if self._uses_oidc(pipeline.cloud):
            # OIDC needs an id-token to be minted for the job; contents:read keeps
            # checkout working under the restricted token (R: cloud OIDC,).
            workflow["permissions"] = {"id-token": "write", "contents": "read"}

        jobs: dict[str, YamlValue] = {}

        # A single security job gathers every requested scan across stages.
        scan_types = self._requested_scans(enabled)
        if scan_types:
            jobs["security"] = self._security_job(scan_types)

        # One job per enabled stage, chained by `needs` to preserve order.
        previous_job: str | None = None
        for stage in enabled:
            jid = _job_id(stage.id)
            jobs[jid] = self._stage_job(
                graph=graph,
                pipeline=pipeline,
                stage=stage,
                env_gate=env_gate,
                needs=previous_job,
                needs_cloud_auth=needs_cloud_auth,
            )
            previous_job = jid

        workflow["jobs"] = jobs
        return workflow

    # ------------------------------------------------------------------
    # Per-stage job
    # ------------------------------------------------------------------
    def _stage_job(
        self,
        *,
        graph: StageGraph,
        pipeline: Pipeline,
        stage: Stage,
        env_gate: dict[str, GateMode],
        needs: str | None,
        needs_cloud_auth: bool,
    ) -> dict[str, YamlValue]:
        """Build one job mapping for a stage (fixed key order)."""
        job: dict[str, YamlValue] = {}
        job["name"] = stage.id

        if needs is not None:
            job["needs"] = needs

        job["runs-on"] = DEFAULT_RUNNER

        # Manual gate OR a manual-gated environment -> reference the GitHub env.
        # Approvals (required reviewers) are enforced at the environment level.
        if stage.environment is not None:
            gated = stage.when is WhenMode.MANUAL or env_gate.get(stage.environment) is GateMode.MANUAL
            # Always reference the environment when a stage targets one (so env
            # protection/reviewers apply); the gate just tells us WHY.
            job["environment"] = stage.environment
            _ = gated  # reference kept explicit for readers; env key covers both
        elif stage.when is WhenMode.MANUAL:
            # Manual stage without a named environment: still express the gate via
            # a dedicated environment reference so a reviewer must approve it.
            job["environment"] = stage.id

        if stage.timeout_minutes is not None:
            job["timeout-minutes"] = stage.timeout_minutes

        # Build the run steps first so we can wire any $SHELL_VAR they reference.
        run_steps = self._run_steps(graph, stage)

        # Map any $SHELL_VAR the target verbs reference into the job env from the
        # matching GitHub secret, so by-reference names (e.g. $LAMBDA_ARTIFACT_BUCKET)
        # actually resolve at run time. Values stay in the secret store — the
        # workflow only wires name -> ${{ secrets.NAME }}. Without this the shell
        # would see empty vars and the aws CLI would fail.
        secret_env = self._referenced_secret_env(run_steps)
        if secret_env:
            job["env"] = secret_env

        # Checkout, optional cloud login, then the stage's run steps.
        steps: list[YamlValue] = [{"name": "Checkout", "uses": ACTION_CHECKOUT}]
        if needs_cloud_auth:
            steps.append(self._cloud_credentials_step(pipeline.cloud))
        steps.extend(run_steps)

        job["steps"] = steps

        if stage.retries and stage.retries > 0:
            # GitHub has no native job retry — record the intent honestly (R: retries).
            job["__comment__"] = Comment(
                f"retries={stage.retries} requested; GitHub Actions has no native job "
                f"retry. Wire a step-level retry action (e.g. nick-fields/retry) if needed."
            )

        return job

    # ------------------------------------------------------------------
    # Run steps — the target verbs OR the IR's stage.runs
    # ------------------------------------------------------------------
    def _run_steps(self, graph: StageGraph, stage: Stage) -> list[YamlValue]:
        """Return the run step(s) for a stage.

 When the stage graph carries target-platform verb commands for this stage
 (the ``build``/``package``/``deploy``/``verify`` shell steps the target
 plugin produced), each command becomes its own ``run`` step so the generated
 workflow actually performs the platform's work — this is what makes "deploy
 once, full pipeline" real. The plugin does NOT inspect what the
 commands do; it only arranges them in order under one job.

 When there are no verb commands (e.g. a scan-only stage, or a CI-plugin unit
 test that built the graph without a target), it falls back to ``stage.runs``
 exactly as before (backward compatible). Multiple commands are joined into a
 single multi-line ``run`` step so the job log stays readable and a failure
 halts the stage (``set -e`` shell semantics apply per GitHub step).
 """
        commands = graph.commands_for(stage)
        if commands:
            # One multi-line run step carrying every verb command in order. A
            # multi-line scalar keeps the emitted YAML compact + deterministic and
            # runs under a single shell so an early failure stops the stage.
            return [{"name": stage.id, "run": "\n".join(commands)}]

        run_cmd = (
            stage.runs if stage.runs is not None else f"echo 'no command for stage {stage.id}'"
        )
        return [{"name": stage.id, "run": run_cmd}]

    # Shell var tokens a verb may reference (e.g. $LAMBDA_ARTIFACT_BUCKET). We map
    # each to a GitHub secret of the same name. Deliberately UPPER_SNAKE only, and
    # we exclude shell-internal vars the commands define themselves (so we don't try
    # to wire a secret for a loop/temp variable).
    _SECRET_VAR_RE = re.compile(r"\$([A-Z][A-Z0-9_]{2,})\b")
    #: Vars the generated commands set/use internally — never mapped to secrets.
    _SHELL_INTERNAL_VARS = frozenset(
        {
            "NEW_VERSION", "NEW_IMAGE", "NEW_TASK_DEF_ARN", "STATE", "STATUS",
            "RUN_ID", "EXEC_ARN", "SM_ARN", "LIFECYCLE", "RESULT", "WHEEL",
            "JOB_ID", "EXISTING", "EXISTING_ARN", "OBJECT_VERSION", "IMAGE_DIGEST",
            "ECR_REGISTRY", "ECR_REPO_NAME", "SCRIPT", "REF", "REF_NOPROTO",
            "S3_PATH", "S3_BUCKET", "S3_KEY", "S3_VERSION", "PATH", "HOME",
        }
    )

    def _referenced_secret_env(
        self, run_steps: list[YamlValue]
    ) -> dict[str, YamlValue]:
        """Map each external ``$VAR`` the steps reference to ``${{ secrets.VAR }}``.

 Scans the run step commands for upper-snake shell variables that are NOT
 set by the commands themselves (those are in :data:`_SHELL_INTERNAL_VARS`)
 and wires them into the job ``env`` from a like-named GitHub secret. This is
 what makes by-reference names (region/bucket/function etc.) resolve at run
 time while the VALUES stay only in the secret store. Deterministic:
 the result is ordered by variable name.
 """
        found: set[str] = set()
        for step in run_steps:
            if isinstance(step, dict):
                run = step.get("run")
                if isinstance(run, str):
                    for m in self._SECRET_VAR_RE.finditer(run):
                        name = m.group(1)
                        if name not in self._SHELL_INTERNAL_VARS:
                            found.add(name)
        return {name: f"${{{{ secrets.{name} }}}}" for name in sorted(found)}

    # ------------------------------------------------------------------
    # Security / scan job
    # ------------------------------------------------------------------
    def _requested_scans(self, stages: list[Stage]) -> tuple[str, ...]:
        """Collect the union of requested scans across stages, in a stable order."""
        requested: set[str] = set()
        for stage in stages:
            for scan in stage.scans or ():
                requested.add(scan.value)
        # Emit in the documented SCAN_STEP_ACTIONS order (deterministic).
        return tuple(s for s in SCAN_STEP_ACTIONS if s in requested)

    def _security_job(self, scan_types: tuple[str, ...]) -> dict[str, YamlValue]:
        """Build the security job that runs each requested scan as a step."""
        steps: list[YamlValue] = [{"name": "Checkout", "uses": ACTION_CHECKOUT}]
        for scan in scan_types:
            step_name, run_cmd = SCAN_STEP_ACTIONS[scan]
            steps.append({"name": step_name, "run": run_cmd})
        return {
            "name": "security",
            "runs-on": DEFAULT_RUNNER,
            "steps": steps,
        }

    # ------------------------------------------------------------------
    # Cloud auth (OIDC keyless, or static access keys)
    # ------------------------------------------------------------------
    def _uses_oidc(self, cloud: Cloud | None) -> bool:
        return cloud is not None and cloud.auth is CloudAuth.OIDC

    def _uses_cloud_auth(self, cloud: Cloud | None) -> bool:
        """True when a cloud login step is needed at all (OIDC or static keys)."""
        return cloud is not None and cloud.auth in (CloudAuth.OIDC, CloudAuth.KEYS)

    def _cloud_credentials_step(self, cloud: Cloud | None) -> dict[str, YamlValue]:
        """Return the provider-appropriate credentials step.

 For OIDC (keyless) the role ARN / WIF identifiers are secret references. For
 static-key auth the AWS access key id + secret are secret references too —
 values live only in GitHub secrets, never in the file.
 """
        assert cloud is not None  # only called when a cloud login is needed
        # Static access-key auth (e.g. where an org SCP forbids OIDC providers).
        if cloud.auth is CloudAuth.KEYS and cloud.provider is CloudProvider.AWS:
            return {
                "name": "Configure AWS credentials (access keys)",
                "uses": ACTION_AWS_OIDC,
                "with": {
                    "aws-access-key-id": f"${{{{ secrets.{AWS_ACCESS_KEY_ID_SECRET} }}}}",
                    "aws-secret-access-key": f"${{{{ secrets.{AWS_SECRET_ACCESS_KEY_SECRET} }}}}",
                    "aws-region": cloud.region if cloud.region else "us-east-1",
                },
            }
        if cloud.provider is CloudProvider.AWS:
            step_with: dict[str, YamlValue] = {
                "role-to-assume": f"${{{{ secrets.{AWS_ROLE_SECRET} }}}}",
                "aws-region": cloud.region if cloud.region else "us-east-1",
            }
            return {
                "name": "Configure AWS credentials (OIDC)",
                "uses": ACTION_AWS_OIDC,
                "with": step_with,
            }
        if cloud.provider is CloudProvider.AZURE:
            return {
                "name": "Azure login (OIDC)",
                "uses": ACTION_AZURE_LOGIN,
                "with": {
                    "client-id": f"${{{{ secrets.{AZURE_CLIENT_ID_SECRET} }}}}",
                    "tenant-id": f"${{{{ secrets.{AZURE_TENANT_ID_SECRET} }}}}",
                    "subscription-id": f"${{{{ secrets.{AZURE_SUBSCRIPTION_ID_SECRET} }}}}",
                },
            }
        # GCP (and any future provider) -> workload identity federation.
        return {
            "name": "Authenticate to Google Cloud (OIDC)",
            "uses": ACTION_GCP_AUTH,
            "with": {
                "workload_identity_provider": f"${{{{ secrets.{GCP_WIF_PROVIDER_SECRET} }}}}",
                "service_account": f"${{{{ secrets.{GCP_SERVICE_ACCOUNT_SECRET} }}}}",
            },
        }
