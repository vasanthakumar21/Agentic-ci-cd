"""Jenkins declarative pipeline emitter.

Renders the vendor-neutral :class:`StageGraph` into a single declarative
``Jenkinsfile`` (a Groovy pipeline). Like the GitHub / Azure DevOps plugins, this
is pure *orchestration*: it arranges stage order, manual gates, approvals,
timeouts, security scans and cloud OIDC — it never decides what a stage *does*.
The command a stage runs is taken verbatim from ``stage.runs`` (the IR); the
target-platform verbs are composed elsewhere at dispatch time.

Why a string builder, not the YAML emitter
-------------------------------------------
A ``Jenkinsfile`` is **Groovy**, not YAML. The GitHub / Azure plugins render YAML
with a small ordered serializer; that is the wrong shape here. Instead this plugin
assembles the declarative pipeline as text with a tiny, explicit Groovy builder
(:class:`_Groovy`) that controls indentation and ordering directly. Determinism comes from the same discipline the YAML plugins use: a fixed, documented
order for every collection, no timestamps, and no hash-ordered iteration.

 — Jenkins column mapping (declarative pipeline)
----------------------------------------------------------
 * stage order -> ``stage('id') {... }`` blocks emitted in declared order
 inside a single ``stages { }`` block. Declarative pipeline runs stages top
 to bottom, so declared order *is* execution order (no ``needs``/``dependsOn``).
 * manual gate -> an ``input`` step (``input message: 'Approve <id>?'``) placed
 before the stage's work for a manual stage OR a manual-gated environment.
 * approvals -> ``input... submitter: '<group>'`` — the reviewer group from
 ``stage.approvals.from_group`` restricts who may approve.
 * timeout -> ``timeout(time: N, unit: 'MINUTES') {... }`` wrapping the
 stage steps (``stage.timeout_minutes``).
 * secrets ref -> ``credentials('NAME')`` via an ``environment { }`` block, by
 reference only — never a literal secret value.
 * cloud OIDC -> a provider-appropriate assume-role / login step referencing a
 credential id by name (AWS -> ``withAWS(roleArn:...)`` style ``sh`` using a
 credential id; Azure/GCP -> an analogous login ``sh`` using a credential id).
 * scans -> any stage requesting ``scans`` contributes a single
 ``security`` stage running a generic ``sh`` step per requested scan
 (sast / secrets / dependency).

``retries`` (honest limitation)
--------------------------------
Jenkins declarative pipeline *does* have a native ``retry(n) { }`` wrapper, so a
stage's ``retries`` maps cleanly: when ``retries > 0`` the stage's steps are
wrapped in ``retry(n) {... }`` (inside the timeout, so each attempt is bounded).
"""

from __future__ import annotations

from cicdgen.ir.models import Cloud, CloudAuth, CloudProvider, GateMode, Pipeline, Stage, WhenMode
from cicdgen.plugins.base import Capability, CIVendorPlugin, GeneratedFile, StageGraph

#: Where the Jenkinsfile is written, relative to the target repo root.
JENKINSFILE_PATH = "Jenkinsfile"

#: Generic scan steps (kept vendor-neutral / placeholder-ish). The intent is a
#: correct *structure*; the exact scanner is a detail the agent can gap-fill.
SCAN_STEP_ACTIONS: dict[str, tuple[str, str]] = {
    # scan type -> (stage label, sh command)
    "sast": ("SAST scan", "echo 'run SAST scan'"),
    "secrets": ("Secret scan", "echo 'run secret scan'"),
    "dependency": ("Dependency scan", "echo 'run dependency scan'"),
}

#: Credential ids referenced for cloud OIDC / assume-role (by reference,).
AWS_ROLE_CREDENTIAL = "AWS_DEPLOY_ROLE_ARN"
AZURE_CREDENTIAL = "AZURE_CREDENTIALS"
GCP_CREDENTIAL = "GCP_WORKLOAD_IDENTITY_PROVIDER"


def _q(value: str) -> str:
    """Single-quote a Groovy string literal, escaping embedded quotes/backslashes.

 Groovy single-quoted strings are *not* interpolated, which is exactly what we
 want for a command taken verbatim from the IR — no accidental ``$var`` or
 ``${expr}`` expansion. We escape ``\\`` and ``'`` so an arbitrary ``stage.runs``
 stays a single, well-formed literal.
 """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


class _Groovy:
    """A tiny deterministic Groovy text builder (fixed 4-space indentation).

 Deliberately minimal: just enough to emit nested ``key {... }`` blocks and
 single ``sh '...'`` / ``input...`` lines with stable indentation. Keeping the
 builder explicit (rather than reaching for a templating engine) makes the
 byte-for-byte determinism easy to reason about — every line is appended
 in a documented order at a known depth.
 """

    INDENT = "    "

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._depth = 0

    def line(self, text: str) -> None:
        """Append one line at the current indentation depth."""
        self._lines.append(f"{self.INDENT * self._depth}{text}" if text else "")

    def open(self, header: str) -> None:
        """Append ``header {`` and indent one level."""
        self.line(f"{header} {{")
        self._depth += 1

    def close(self) -> None:
        """Dedent one level and append the closing ``}``."""
        self._depth -= 1
        self.line("}")

    def render(self) -> str:
        """Return the assembled text with a trailing newline (stable output)."""
        return "\n".join(self._lines) + "\n"


class Jenkins(CIVendorPlugin):
    """Emit a declarative ``Jenkinsfile`` from a stage graph.

 ``name`` MUST equal the directory name ``plugins/ci/jenkins`` so the IR value,
 the folder, and this class all agree (see base.py module docstring,).
 """

    name = "jenkins"

    #: Pipeline-expression capabilities Jenkins can render: input-step
    #: approvals + manual gates, cloud OIDC via assume-role, self-hosted agents,
    #: matrix (``axes``) builds, and scan stages. Secrets stay references.
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
        """Render ``Jenkinsfile`` for ``graph``.

 Builds one ``stage('id') { }`` block per *enabled* stage (in declared
 order) inside a single ``stages { }``, an optional ``security`` stage when
 any stage requests scans, an ``environment { }`` of ``credentials('X')``
 references for secrets, and provider-appropriate OIDC/assume-role steps
 when ``cloud.auth == oidc``.
 """
        pipeline = graph.pipeline
        enabled = [s for s in graph.stages if s.enabled]
        env_gate = {e.name: e.gate for e in pipeline.environments}

        content = self._build_jenkinsfile(pipeline, enabled, env_gate)
        return (GeneratedFile(path=JENKINSFILE_PATH, content=content),)

    # ------------------------------------------------------------------
    # Pipeline assembly (ordered, deterministic)
    # ------------------------------------------------------------------
    def _build_jenkinsfile(
        self,
        pipeline: Pipeline,
        enabled: list[Stage],
        env_gate: dict[str, GateMode],
    ) -> str:
        """Assemble the whole declarative pipeline text in a fixed order."""
        g = _Groovy()
        oidc = self._uses_oidc(pipeline.cloud)

        g.open("pipeline")
        g.line("agent any")

        # environment { } holds secret references via credentials('NAME') — by
        # reference only. Only emitted when there is something to bind.
        credential_ids = self._credential_ids(pipeline, oidc)
        if credential_ids:
            g.open("environment")
            for env_var, cred_id in credential_ids:
                g.line(f"{env_var} = credentials({_q(cred_id)})")
            g.close()

        g.open("stages")

        # A single security stage gathers every requested scan; it heads the
        # pipeline so scans run before the ordered stages (declared order).
        scan_types = self._requested_scans(enabled)
        if scan_types:
            self._emit_security_stage(g, scan_types)

        # Scan-only stages (request scans but have no command) are represented
        # entirely by the synthetic security stage; a stage carrying a command
        # still gets its own stage (its scans are additionally folded in above).
        for stage in enabled:
            if self._is_scan_only(stage):
                continue
            self._emit_stage(g, pipeline, stage, env_gate, oidc)

        g.close()  # stages
        g.close()  # pipeline
        return g.render()

    # ------------------------------------------------------------------
    # Per-stage
    # ------------------------------------------------------------------
    def _emit_stage(
        self,
        g: _Groovy,
        pipeline: Pipeline,
        stage: Stage,
        env_gate: dict[str, GateMode],
        oidc: bool,
    ) -> None:
        """Emit one ``stage('id') {... }`` block (fixed inner order)."""
        g.open(f"stage({_q(stage.id)})")
        g.open("steps")

        # Manual gate OR a manual-gated environment -> an input step first.
        if self._is_gated(stage, env_gate):
            self._emit_input(g, stage)

        # The stage body (OIDC login + the command), optionally wrapped in a
        # timeout and/or a retry. Timeout is the outer wrapper so each retry
        # attempt is individually bounded.
        if stage.timeout_minutes is not None:
            g.open(f"timeout(time: {stage.timeout_minutes}, unit: 'MINUTES')")
            self._emit_stage_body(g, pipeline, stage, oidc)
            g.close()
        else:
            self._emit_stage_body(g, pipeline, stage, oidc)

        g.close()  # steps
        g.close()  # stage

    def _emit_stage_body(
        self, g: _Groovy, pipeline: Pipeline, stage: Stage, oidc: bool
    ) -> None:
        """Emit the OIDC login step (if any) + the run command, with retry wrap."""
        if stage.retries and stage.retries > 0:
            g.open(f"retry({stage.retries})")
            self._emit_run_steps(g, pipeline, stage, oidc)
            g.close()
        else:
            self._emit_run_steps(g, pipeline, stage, oidc)

    def _emit_run_steps(
        self, g: _Groovy, pipeline: Pipeline, stage: Stage, oidc: bool
    ) -> None:
        """Emit the cloud login step (if OIDC) then the stage's sh command."""
        if oidc:
            g.line(f"sh {_q(self._cloud_login_command(pipeline.cloud))}")
        run_cmd = stage.runs if stage.runs is not None else f"echo 'no command for stage {stage.id}'"
        g.line(f"sh {_q(run_cmd)}")

    def _emit_input(self, g: _Groovy, stage: Stage) -> None:
        """Emit the manual-gate ``input`` step, with a submitter when approvals set.

 ``input message: 'Approve <id>?'`` pauses the pipeline for a human; when
 the stage declares an approver group it becomes
 ``input message: '...', submitter: '<group>'`` so only that group may
 approve.
 """
        parts = [f"message: {_q(f'Approve {stage.id}?')}"]
        if stage.approvals is not None and stage.approvals.from_group:
            parts.append(f"submitter: {_q(stage.approvals.from_group)}")
        g.line(f"input {', '.join(parts)}")

    def _is_gated(self, stage: Stage, env_gate: dict[str, GateMode]) -> bool:
        """True when a stage needs a manual ``input`` gate.

 A stage is gated when it is explicitly ``when=manual`` OR it targets an
 environment whose ``gate`` is manual.
 """
        if stage.when is WhenMode.MANUAL:
            return True
        if stage.environment is not None and env_gate.get(stage.environment) is GateMode.MANUAL:
            return True
        return False

    # ------------------------------------------------------------------
    # Security / scan stage
    # ------------------------------------------------------------------
    def _is_scan_only(self, stage: Stage) -> bool:
        """True when a stage only requests scans and has no command to run.

 Such a stage is represented entirely by the synthetic ``security`` stage
 (its scans are folded in), so it must not also emit its own stage. A stage
 with both ``scans`` and ``runs`` is NOT scan-only — it still needs its own
 stage for the command.
 """
        return bool(stage.scans) and stage.runs is None

    def _requested_scans(self, stages: list[Stage]) -> tuple[str, ...]:
        """Collect the union of requested scans across stages, in a stable order."""
        requested: set[str] = set()
        for stage in stages:
            for scan in stage.scans or ():
                requested.add(scan.value)
        # Emit in the documented SCAN_STEP_ACTIONS order (deterministic).
        return tuple(s for s in SCAN_STEP_ACTIONS if s in requested)

    def _emit_security_stage(self, g: _Groovy, scan_types: tuple[str, ...]) -> None:
        """Emit the ``security`` stage running each requested scan as a ``sh`` step."""
        g.open("stage('security')")
        g.open("steps")
        for scan in scan_types:
            _, run_cmd = SCAN_STEP_ACTIONS[scan]
            g.line(f"sh {_q(run_cmd)}")
        g.close()  # steps
        g.close()  # stage

    # ------------------------------------------------------------------
    # Secrets / cloud OIDC
    # ------------------------------------------------------------------
    def _credential_ids(self, pipeline: Pipeline, oidc: bool) -> tuple[tuple[str, str], ...]:
        """The (env-var, credential-id) pairs to bind in ``environment { }``.

 Currently this is the cloud credential referenced for OIDC/assume-role;
 every value is a credential *id* resolved by Jenkins at runtime via
 ``credentials('id')`` — never a literal secret.
 """
        if not oidc or pipeline.cloud is None:
            return ()
        provider = pipeline.cloud.provider
        if provider is CloudProvider.AWS:
            return (("AWS_ROLE_ARN", AWS_ROLE_CREDENTIAL),)
        if provider is CloudProvider.AZURE:
            return (("AZURE_CREDENTIALS", AZURE_CREDENTIAL),)
        return (("GCP_WORKLOAD_IDENTITY_PROVIDER", GCP_CREDENTIAL),)

    def _uses_oidc(self, cloud: Cloud | None) -> bool:
        return cloud is not None and cloud.auth is CloudAuth.OIDC

    def _cloud_login_command(self, cloud: Cloud | None) -> str:
        """Return the provider-appropriate assume-role / login shell command.

 All credentials are referenced through the ``environment { }`` binding
 (``credentials('id')``) exposed as an env var — the command references the
 env var, never a literal secret. The AWS form is an
 assume-role style step analogous to ``withAWS``; Azure/GCP are analogous
 login commands that read their credential id from the environment.
 """
        assert cloud is not None  # only called when _uses_oidc is True
        if cloud.provider is CloudProvider.AWS:
            region = cloud.region if cloud.region else "us-east-1"
            return (
                f"aws sts assume-role-with-web-identity --role-arn $AWS_ROLE_ARN "
                f"--role-session-name jenkins --region {region}"
            )
        if cloud.provider is CloudProvider.AZURE:
            return "az login --federated-token $AZURE_CREDENTIALS"
        return "gcloud auth login --cred-file=$GCP_WORKLOAD_IDENTITY_PROVIDER"
