"""Bitbucket Pipelines emitter.

Renders the vendor-neutral :class:`StageGraph` into a single Bitbucket Pipelines
file, ``bitbucket-pipelines.yml``. Like the GitHub and Azure DevOps plugins, this
is pure *orchestration*: it arranges stage order, manual gates, approvals,
timeouts, security scans and cloud OIDC — it never decides what a stage *does*.
The command a stage runs is taken verbatim from ``stage.runs`` (the IR); the
target-platform verbs are composed elsewhere at dispatch time.

 — Bitbucket column mapping
-------------------------------------
 * stage order -> ``pipelines.default`` is a *sequence* of ``- step:`` entries
 that Bitbucket runs top-to-bottom. Each enabled IR stage becomes one
 ``- step:`` in declared order, so the order is the list order (no explicit
 dependency keys — Bitbucket steps are inherently sequential).
 * manual gate -> ``trigger: manual`` on the step. A manual stage (or a stage
 targeting a manual-gated environment) pauses the pipeline until a user
 triggers it in the Bitbucket UI.
 * approvals -> deployment permissions on the environment, referenced via
 ``deployment: <env>`` on the step. Required reviewers / deployment
 permissions are configured on the Bitbucket *environment*; the pipeline
 only *references* it (the approval lives with the environment, not the YAML).
 * timeout -> ``max-time:`` on the step, in minutes (``timeout_minutes``).
 * secrets ref -> ``$NAME`` repository/workspace variables — by reference only,
 never values.
 * cloud OIDC -> Bitbucket OIDC identity. We emit a provider-appropriate login
 step that references the OIDC identity token / role by variable ($NAME),
 never a literal secret. ``oidc: true`` is set on the step so Bitbucket mints
 the identity token.
 * scans -> any stage requesting ``scans`` contributes a single leading
 ``security`` step running each requested scan (SAST / secret / dependency),
 kept generic.

Determinism
------------------
The pipeline must regenerate byte-for-byte. We emit YAML with a small, explicit,
ordered serializer (:mod:`.yaml_emit`) instead of a general YAML dumper whose key
order / style can drift. There are no timestamps and every collection is built in
a fixed, documented order.

``retries`` (honest limitation)
--------------------------------
Bitbucket Pipelines supports ``max-time`` but has no simple declarative step
*retry* count that maps cleanly to the IR intent. When a stage sets
``retries > 0`` we attach an in-band comment noting the limitation rather than
silently dropping the intent (a ``after-script`` re-run or a step-level script
loop could be wired in later).
"""

from __future__ import annotations

from cicdgen.ir.models import Cloud, CloudAuth, CloudProvider, GateMode, Pipeline, Stage, WhenMode
from cicdgen.plugins.base import Capability, CIVendorPlugin, GeneratedFile, StageGraph

from .yaml_emit import Comment, YamlValue, dump_mapping

#: Where the pipeline is written, relative to the target repo root.
PIPELINE_PATH = "bitbucket-pipelines.yml"

#: The default Docker image steps run in. A fixed, documented default keeps output
#: deterministic; a future IR knob could override it.
DEFAULT_IMAGE = "atlassian/default-image:4"

#: Generic scan step definitions (kept vendor-neutral / placeholder-ish). The
#: intent is a correct *structure*; the exact scanner is a detail the agent can
#: gap-fill.
SCAN_STEP_ACTIONS: dict[str, tuple[str, str]] = {
    # scan type -> (step name, script command)
    "sast": ("SAST scan", "echo 'run SAST scan'"),
    "secrets": ("Secret scan", "echo 'run secret scan'"),
    "dependency": ("Dependency scan", "echo 'run dependency scan'"),
}

#: Variable names referenced for cloud OIDC credentials (by reference,).
AWS_ROLE_VAR = "AWS_DEPLOY_ROLE_ARN"
AZURE_CLIENT_ID_VAR = "AZURE_CLIENT_ID"
AZURE_TENANT_ID_VAR = "AZURE_TENANT_ID"
AZURE_SUBSCRIPTION_ID_VAR = "AZURE_SUBSCRIPTION_ID"
GCP_WIF_PROVIDER_VAR = "GCP_WORKLOAD_IDENTITY_PROVIDER"
GCP_SERVICE_ACCOUNT_VAR = "GCP_SERVICE_ACCOUNT"


class Bitbucket(CIVendorPlugin):
    """Emit a Bitbucket Pipelines file from a stage graph.

 ``name`` MUST equal the directory name ``plugins/ci/bitbucket`` so the IR
 value, the folder, and this class all agree (see base.py module docstring,).
 """

    name = "bitbucket"

    #: Pipeline-expression capabilities Bitbucket Pipelines can render:
    #: deployment-permission approvals, ``trigger: manual`` gates, OIDC identity,
    #: self-hosted runners, parallel/matrix steps, and scan steps. Secrets stay
    #: references via repository variables.
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
        """Render ``bitbucket-pipelines.yml`` for ``graph``.

 Builds one ``- step:`` per *enabled* stage (preserving declared order as
 the list order), an optional leading ``security`` step when any stage
 requests scans, and wires cloud OIDC login steps when
 ``cloud.auth == oidc``.
 """
        pipeline = graph.pipeline
        enabled = [s for s in graph.stages if s.enabled]
        env_gate = {e.name: e.gate for e in pipeline.environments}

        document = self._build_pipeline(pipeline, enabled, env_gate)
        content = dump_mapping(document)
        return (GeneratedFile(path=PIPELINE_PATH, content=content),)

    # ------------------------------------------------------------------
    # Pipeline assembly (ordered, deterministic)
    # ------------------------------------------------------------------
    def _build_pipeline(
        self,
        pipeline: Pipeline,
        enabled: list[Stage],
        env_gate: dict[str, GateMode],
    ) -> dict[str, YamlValue]:
        """Assemble the whole pipeline mapping in a fixed key order."""
        document: dict[str, YamlValue] = {}

        # A fixed default image keeps the output deterministic.
        document["image"] = DEFAULT_IMAGE

        oidc = self._uses_oidc(pipeline.cloud)

        steps: list[YamlValue] = []

        # A single security step gathers every requested scan across stages; it
        # heads the sequence so scans run before the ordered stages.
        scan_types = self._requested_scans(enabled)
        if scan_types:
            steps.append(self._security_step(scan_types))

        # Scan-only stages (request scans but have no command to run) are folded
        # entirely into the single ``security`` step above; emitting them again
        # as their own build step would duplicate the scan work and produce a step
        # with nothing to do. A stage that also carries a ``runs`` command still
        # gets its own step (its scans are additionally folded in).
        buildable = [s for s in enabled if not self._is_scan_only(s)]

        # One ``- step:`` per enabled, buildable IR stage, in declared order.
        for stage in buildable:
            steps.append(
                self._step_entry(
                    pipeline=pipeline,
                    stage=stage,
                    env_gate=env_gate,
                    oidc=oidc,
                )
            )

        # Bitbucket's top-level ``pipelines:`` key with ``default:`` holding the
        # ordered step sequence. ``default`` runs on every push not matched by a
        # more specific ``branches:`` entry.
        document["pipelines"] = {"default": steps}
        return document

    # ------------------------------------------------------------------
    # Per-stage step
    # ------------------------------------------------------------------
    def _step_entry(
        self,
        *,
        pipeline: Pipeline,
        stage: Stage,
        env_gate: dict[str, GateMode],
        oidc: bool,
    ) -> dict[str, YamlValue]:
        """Build one ``- step:`` entry for an IR stage (fixed key order).

 Returns the ``{"step": {...}}`` wrapper Bitbucket expects as a sequence
 item under ``pipelines.default``.
 """
        step: dict[str, YamlValue] = {}
        step["name"] = stage.id

        # Deployment target: a named environment binds the step to Bitbucket's
        # deployment permissions (approvals live on the environment). A manual
        # stage without a named environment still gates via trigger: manual.
        if stage.environment is not None:
            step["deployment"] = stage.environment

        # Manual gate: explicit when=manual OR a manual-gated environment.
        gated = stage.when is WhenMode.MANUAL or (
            stage.environment is not None
            and env_gate.get(stage.environment) is GateMode.MANUAL
        )
        if gated:
            step["trigger"] = "manual"
            if stage.environment is not None:
                step["__comment__"] = Comment(
                    f"manual gate: configure deployment permissions / required "
                    f"reviewers on the '{stage.environment}' environment in Bitbucket; "
                    f"the step waits for a manual trigger."
                )

        if stage.timeout_minutes is not None:
            step["max-time"] = stage.timeout_minutes

        if oidc:
            # Bitbucket mints an OIDC identity token for the step when oidc: true.
            step["oidc"] = True

        step["script"] = self._step_script(pipeline, stage, oidc)

        if stage.retries and stage.retries > 0:
            # Bitbucket has no clean declarative step-retry count — note the intent
            # honestly rather than dropping it (R: retries).
            existing = step.get("__comment__")
            note = (
                f"retries={stage.retries} requested; Bitbucket Pipelines has no "
                f"native declarative step retry — wire a script-level retry if needed."
            )
            if isinstance(existing, Comment):
                step["__comment__"] = Comment(f"{existing.text} {note}")
            else:
                step["__comment__"] = Comment(note)

        return {"step": step}

    def _step_script(
        self, pipeline: Pipeline, stage: Stage, oidc: bool
    ) -> list[YamlValue]:
        """Build the ordered ``script:`` command list for a stage.

 An OIDC login command (referencing credentials by ``$NAME`` only) is
 prepended when the cloud uses OIDC, followed by the stage's ``runs``.
 """
        script: list[YamlValue] = []
        if oidc:
            script.append(self._cloud_credentials_command(pipeline.cloud))
        run_cmd = stage.runs if stage.runs is not None else f"echo 'no command for stage {stage.id}'"
        script.append(run_cmd)
        return script

    # ------------------------------------------------------------------
    # Security / scan step
    # ------------------------------------------------------------------
    def _is_scan_only(self, stage: Stage) -> bool:
        """True when a stage only requests scans and has no command to run.

 Such a stage is represented entirely by the synthetic ``security`` step
 (its scans are folded in), so it must not also emit its own build step. A
 stage with both ``scans`` and ``runs`` is NOT scan-only — it still needs
 its own step for the command.
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

    def _security_step(self, scan_types: tuple[str, ...]) -> dict[str, YamlValue]:
        """Build the security step that runs each requested scan as a command."""
        script: list[YamlValue] = []
        for scan in scan_types:
            _, run_cmd = SCAN_STEP_ACTIONS[scan]
            script.append(run_cmd)
        step: dict[str, YamlValue] = {
            "name": "security",
            "script": script,
        }
        return {"step": step}

    # ------------------------------------------------------------------
    # Cloud OIDC
    # ------------------------------------------------------------------
    def _uses_oidc(self, cloud: Cloud | None) -> bool:
        return cloud is not None and cloud.auth is CloudAuth.OIDC

    def _cloud_credentials_command(self, cloud: Cloud | None) -> str:
        """Return the provider-appropriate OIDC login command.

 All identifiers (role ARN, client/tenant/subscription ids, WIF provider)
 are referenced as ``$NAME`` repository/workspace variables only — never
 literal secrets. The step's ``oidc: true`` makes Bitbucket mint
 the identity token Bitbucket exposes as ``$BITBUCKET_STEP_OIDC_TOKEN``.
 """
        assert cloud is not None  # only called when _uses_oidc is True
        if cloud.provider is CloudProvider.AWS:
            region = cloud.region if cloud.region else "us-east-1"
            return (
                "aws sts assume-role-with-web-identity "
                f"--role-arn ${AWS_ROLE_VAR} "
                "--role-session-name bitbucket "
                "--web-identity-token $BITBUCKET_STEP_OIDC_TOKEN "
                f"--region {region}"
            )
        if cloud.provider is CloudProvider.AZURE:
            return (
                "az login --service-principal "
                f"--username ${AZURE_CLIENT_ID_VAR} "
                f"--tenant ${AZURE_TENANT_ID_VAR} "
                "--federated-token $BITBUCKET_STEP_OIDC_TOKEN "
                f"&& az account set --subscription ${AZURE_SUBSCRIPTION_ID_VAR}"
            )
        # GCP (and any future provider) -> workload identity federation; provider
        # and service account referenced by variable, token from Bitbucket OIDC.
        return (
            "gcloud iam workload-identity-pools create-cred-config "
            f"${GCP_WIF_PROVIDER_VAR} "
            f"--service-account=${GCP_SERVICE_ACCOUNT_VAR} "
            "--credential-source-file=$BITBUCKET_STEP_OIDC_TOKEN "
            "--output-file=gcp-creds.json"
        )
