"""Azure Pipelines emitter.

Renders the vendor-neutral :class:`StageGraph` into a single Azure Pipelines
file, ``azure-pipelines.yml``. Like the GitHub plugin, this is pure
*orchestration*: it arranges stage order, manual gates, approvals,
timeouts, security scans and cloud OIDC — it never decides what a stage *does*.
The command a stage runs is taken verbatim from ``stage.runs`` (the IR); the
target-platform verbs are composed elsewhere at dispatch time.

 — Azure DevOps column mapping
----------------------------------------
 * stage order -> ``stages`` with ``dependsOn``. Each IR stage becomes one
 Azure ``stage`` whose ``dependsOn`` is the previous *enabled* stage, so the
 declared order is enforced as a linear chain (a security stage, when
 present, is the chain's head).
 * manual gate -> a stage that is ``when=manual`` OR targets an environment
 whose ``gate=manual`` uses a ``deployment`` job bound to an Azure DevOps
 ``environment:``. The approval *check* is configured on that environment in
 Azure DevOps (by the bootstrap step); the pipeline only *references* it.
 * approvals -> same ``environment:`` reference (environment approvals/checks
 enforce the required reviewers).
 * timeout -> ``timeoutInMinutes`` on the job (``stage.timeout_minutes``).
 * secrets ref -> ``$(NAME)`` macro syntax. Secret *values* live in a variable
 group linked at the project level (referenced via ``variables: - group:``);
 the pipeline names them by reference only, never values.
 * cloud OIDC -> workload identity federation via a *service connection*. We
 emit a provider-appropriate login step (AWS -> ``AWSShellScript@1`` using an
 AWS service connection; Azure -> ``AzureCLI@2`` with a WIF service
 connection; GCP -> a ``gcloud`` auth step via a service connection). The
 service-connection name and any identifiers are referenced by ``$(var)`` /
 a documented connection name, never literal secrets.
 * scans -> any stage requesting ``scans`` contributes jobs/steps to a
 single ``security`` stage (SAST / secret / dependency scan steps).

Determinism
------------------
The pipeline must regenerate byte-for-byte. We emit YAML with a small, explicit,
ordered serializer (:mod:`.yaml_emit`) instead of a general YAML dumper whose key
order / style can drift. There are no timestamps and every collection is built in
a fixed, documented order.

``retries`` (honest limitation)
--------------------------------
Azure Pipelines supports step-level ``retryCountOnTaskFailure`` but has no simple
*stage/job* retry knob that maps cleanly to the IR intent. When a stage sets
``retries > 0`` we attach ``retryCountOnTaskFailure`` to the run step and note it,
rather than silently dropping the intent.
"""

from __future__ import annotations

from cicdgen.ir.models import Cloud, CloudAuth, CloudProvider, GateMode, Pipeline, Stage, WhenMode
from cicdgen.plugins.base import Capability, CIVendorPlugin, GeneratedFile, StageGraph

from .yaml_emit import Comment, YamlValue, dump_mapping

#: Where the pipeline is written, relative to the target repo root.
PIPELINE_PATH = "azure-pipelines.yml"

#: The agent pool image used for every job. A fixed, documented default keeps
#: output deterministic; a future IR knob could override it.
DEFAULT_VM_IMAGE = "ubuntu-latest"

#: The variable group that holds secret *values* (configured at the project
#: level in Azure DevOps). The pipeline links it and references members as $(X).
DEFAULT_VARIABLE_GROUP = "cicd-secrets"

#: Pinned task references. Pinning keeps the emitted pipeline deterministic.
TASK_AZURE_CLI = "AzureCLI@2"
TASK_AWS_SHELL = "AWSShellScript@1"

#: Generic scan step definitions (kept vendor-neutral / placeholder-ish). The
#: intent is a correct *structure*; the exact scanner is a detail the agent can
#: gap-fill.
SCAN_STEP_ACTIONS: dict[str, tuple[str, str]] = {
    # scan type -> (display name, script command)
    "sast": ("SAST scan", "echo 'run SAST scan'"),
    "secrets": ("Secret scan", "echo 'run secret scan'"),
    "dependency": ("Dependency scan", "echo 'run dependency scan'"),
}

#: Service-connection names referenced for cloud OIDC / WIF (by reference only).
AWS_SERVICE_CONNECTION = "aws-oidc"
AZURE_SERVICE_CONNECTION = "azure-wif"
GCP_SERVICE_CONNECTION = "gcp-wif"


def _stage_name(stage_id: str) -> str:
    """Map a stage id to an Azure Pipelines stage/job name.

 Azure identifiers must match ``[A-Za-z_][A-Za-z0-9_]*`` (no ``-``). Stage ids
 are user-defined, so we replace any disallowed char with ``_`` deterministically
 and prefix ``s_`` when the id does not start with a letter/underscore.
 """
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in stage_id)
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"s_{cleaned}"
    return cleaned


class AzureDevOps(CIVendorPlugin):
    """Emit an Azure Pipelines file from a stage graph.

 ``name`` MUST equal the directory name ``plugins/ci/azure-devops`` so the IR
 value, the folder, and this class all agree (see base.py module docstring,).
 """

    name = "azure-devops"

    #: Pipeline-expression capabilities Azure DevOps can render: env
    #: approvals/checks, manual-gated environments, workload identity federation
    #: (OIDC), self-hosted agent pools, matrix (``strategy``) builds, and scan
    #: stages. Secrets stay references via variable groups.
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
        """Render ``azure-pipelines.yml`` for ``graph``.

 Builds one Azure stage per *enabled* IR stage (preserving declared order
 via ``dependsOn``), an optional ``security`` stage when any stage requests
 scans, and wires cloud OIDC / WIF login steps when ``cloud.auth == oidc``.
 """
        pipeline = graph.pipeline
        enabled = [s for s in graph.stages if s.enabled]
        env_gate = {e.name: e.gate for e in pipeline.environments}

        document = self._build_pipeline(graph, pipeline, enabled, env_gate)
        content = dump_mapping(document)
        return (GeneratedFile(path=PIPELINE_PATH, content=content),)

    # ------------------------------------------------------------------
    # Pipeline assembly (ordered, deterministic)
    # ------------------------------------------------------------------
    def _build_pipeline(
        self,
        graph: StageGraph,
        pipeline: Pipeline,
        enabled: list[Stage],
        env_gate: dict[str, GateMode],
    ) -> dict[str, YamlValue]:
        """Assemble the whole pipeline mapping in a fixed key order."""
        document: dict[str, YamlValue] = {}

        # Trigger: the IR's ``triggers`` block drives which branches start the
        # pipeline (branch-per-ticket etc.). Absent => main-only default (backward
        # compatible). Azure uses ``trigger.branches.include`` for the push trigger.
        push_branches = ["main"]
        if pipeline.triggers is not None:
            push_branches = list(pipeline.triggers.push_branches)
        document["trigger"] = {"branches": {"include": push_branches}}

        # Variable group carries secret values (linked at the project level); the
        # pipeline references members as $(NAME). Documented in a comment so a
        # reader knows where secrets come from.
        document["variables"] = [
            {
                "__comment__": Comment(
                    f"secret values come from the '{DEFAULT_VARIABLE_GROUP}' variable group "
                    f"linked at the project level; reference members as $(NAME)."
                ),
                "group": DEFAULT_VARIABLE_GROUP,
            }
        ]

        stages: list[YamlValue] = []

        oidc = self._uses_oidc(pipeline.cloud)

        # A single security stage gathers every requested scan across stages; it
        # heads the dependsOn chain so scans run before the ordered stages.
        previous_stage: str | None = None
        scan_types = self._requested_scans(enabled)
        if scan_types:
            stages.append(self._security_stage(scan_types))
            previous_stage = "security"

        # Scan-only stages (request scans but have no command to run) are folded
        # entirely into the single ``security`` stage above; emitting them again
        # as their own build stage would (a) duplicate the ``security`` stage name
        # when the IR stage id is literally ``security`` and (b) produce a stage
        # with nothing to do. A stage that also carries a ``runs`` command still
        # gets its own build stage (its scans are additionally folded in).
        buildable = [s for s in enabled if not self._is_scan_only(s)]

        # One Azure stage per enabled, buildable IR stage, chained by dependsOn.
        for stage in buildable:
            sname = _stage_name(stage.id)
            stages.append(
                self._stage(
                    graph=graph,
                    pipeline=pipeline,
                    stage=stage,
                    env_gate=env_gate,
                    depends_on=previous_stage,
                    oidc=oidc,
                )
            )
            previous_stage = sname

        document["stages"] = stages
        return document

    # ------------------------------------------------------------------
    # Per-stage
    # ------------------------------------------------------------------
    def _stage(
        self,
        *,
        graph: StageGraph,
        pipeline: Pipeline,
        stage: Stage,
        env_gate: dict[str, GateMode],
        depends_on: str | None,
        oidc: bool,
    ) -> dict[str, YamlValue]:
        """Build one Azure ``stage`` mapping for an IR stage (fixed key order).

 Env-targeted or manual stages use a ``deployment`` job bound to an Azure
 DevOps environment (so environment approvals/checks apply); every other
 stage uses a plain ``job``.
 """
        sname = _stage_name(stage.id)
        azure_stage: dict[str, YamlValue] = {}
        azure_stage["stage"] = sname
        azure_stage["displayName"] = stage.id

        if depends_on is not None:
            azure_stage["dependsOn"] = depends_on

        # Decide gated vs plain.
        environment = stage.environment
        if environment is None and stage.when is WhenMode.MANUAL:
            # Manual stage without a named environment: still bind to an
            # environment so an approval check can gate it.
            environment = stage.id
        gated = environment is not None and (
            stage.when is WhenMode.MANUAL
            or (stage.environment is not None and env_gate.get(stage.environment) is GateMode.MANUAL)
        )

        steps = self._stage_steps(graph, pipeline, stage, oidc)

        if environment is not None:
            # Deployment job bound to an Azure DevOps environment. Approvals /
            # checks are configured on the environment in Azure DevOps.
            job = self._deployment_job(stage, environment, steps, gated)
            azure_stage["jobs"] = [job]
        else:
            job = self._plain_job(stage, steps)
            azure_stage["jobs"] = [job]

        return azure_stage

    def _plain_job(self, stage: Stage, steps: list[YamlValue]) -> dict[str, YamlValue]:
        """A regular build job (fixed key order)."""
        job: dict[str, YamlValue] = {}
        job["job"] = _stage_name(stage.id)
        job["displayName"] = stage.id
        if stage.timeout_minutes is not None:
            job["timeoutInMinutes"] = stage.timeout_minutes
        job["pool"] = {"vmImage": DEFAULT_VM_IMAGE}
        job["steps"] = steps
        return job

    def _deployment_job(
        self,
        stage: Stage,
        environment: str,
        steps: list[YamlValue],
        gated: bool,
    ) -> dict[str, YamlValue]:
        """A deployment job bound to an environment (fixed key order).

 The ``environment:`` reference is what makes Azure DevOps enforce the
 environment's approvals/checks; a ``runOnce`` strategy carries the steps.
 """
        job: dict[str, YamlValue] = {}
        job["deployment"] = _stage_name(stage.id)
        job["displayName"] = stage.id
        if stage.timeout_minutes is not None:
            job["timeoutInMinutes"] = stage.timeout_minutes
        job["pool"] = {"vmImage": DEFAULT_VM_IMAGE}
        job["environment"] = environment
        if gated:
            job["__comment__"] = Comment(
                f"manual gate: configure an approval check on the '{environment}' "
                f"environment in Azure DevOps; the deployment waits for it."
            )
        job["strategy"] = {"runOnce": {"deploy": {"steps": steps}}}
        return job

    def _stage_steps(
        self, graph: StageGraph, pipeline: Pipeline, stage: Stage, oidc: bool
    ) -> list[YamlValue]:
        """Build the ordered step list for a stage (checkout, OIDC login, run).

 When the stage graph carries target-platform verb commands for this stage,
 those commands ARE the stage's work (the ``build``/``deploy``/… steps the
 target plugin produced) and become the ``script`` body — joined into one
 multi-line script so the generated pipeline runs the platform's work and a
 failure halts the stage. The plugin never inspects what the commands
 do. With no verb commands it falls back to ``stage.runs`` exactly as
 before (backward compatible).
 """
        steps: list[YamlValue] = [{"checkout": "self"}]
        if oidc:
            steps.append(self._cloud_credentials_step(pipeline.cloud))

        commands = graph.commands_for(stage)
        if commands:
            run_cmd = "\n".join(commands)
        else:
            run_cmd = (
                stage.runs if stage.runs is not None else f"echo 'no command for stage {stage.id}'"
            )
        run_step: dict[str, YamlValue] = {
            "script": run_cmd,
            "displayName": stage.id,
        }
        if stage.retries and stage.retries > 0:
            # Azure has no clean stage/job retry; use step-level retry and note it.
            run_step["retryCountOnTaskFailure"] = stage.retries
            run_step["__comment__"] = Comment(
                f"retries={stage.retries} requested; Azure Pipelines has no native "
                f"stage retry — using step-level retryCountOnTaskFailure."
            )
        steps.append(run_step)
        return steps

    # ------------------------------------------------------------------
    # Security / scan stage
    # ------------------------------------------------------------------
    def _is_scan_only(self, stage: Stage) -> bool:
        """True when a stage only requests scans and has no command to run.

 Such a stage is represented entirely by the synthetic ``security`` stage
 (its scans are folded in), so it must not also emit its own build stage.
 A stage with both ``scans`` and ``runs`` is NOT scan-only — it still needs
 its own stage for the command.
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

    def _security_stage(self, scan_types: tuple[str, ...]) -> dict[str, YamlValue]:
        """Build the security stage that runs each requested scan as a step."""
        steps: list[YamlValue] = [{"checkout": "self"}]
        for scan in scan_types:
            step_name, run_cmd = SCAN_STEP_ACTIONS[scan]
            steps.append({"script": run_cmd, "displayName": step_name})
        job: dict[str, YamlValue] = {
            "job": "security",
            "displayName": "security",
            "pool": {"vmImage": DEFAULT_VM_IMAGE},
            "steps": steps,
        }
        return {
            "stage": "security",
            "displayName": "security",
            "jobs": [job],
        }

    # ------------------------------------------------------------------
    # Cloud OIDC / workload identity federation
    # ------------------------------------------------------------------
    def _uses_oidc(self, cloud: Cloud | None) -> bool:
        return cloud is not None and cloud.auth is CloudAuth.OIDC

    def _cloud_credentials_step(self, cloud: Cloud | None) -> dict[str, YamlValue]:
        """Return the provider-appropriate WIF / OIDC login step.

 All credentials come from a *service connection* (configured in Azure
 DevOps with workload identity federation) referenced by name, plus any
 identifiers referenced as ``$(var)`` — never literal secrets.
 """
        assert cloud is not None  # only called when _uses_oidc is True
        if cloud.provider is CloudProvider.AWS:
            region = cloud.region if cloud.region else "us-east-1"
            return {
                "task": TASK_AWS_SHELL,
                "displayName": "Configure AWS credentials (OIDC)",
                "inputs": {
                    "awsCredentials": AWS_SERVICE_CONNECTION,
                    "regionName": region,
                    "scriptType": "inline",
                    "inlineScript": "aws sts get-caller-identity",
                },
            }
        if cloud.provider is CloudProvider.AZURE:
            return {
                "task": TASK_AZURE_CLI,
                "displayName": "Azure login (workload identity federation)",
                "inputs": {
                    "azureSubscription": AZURE_SERVICE_CONNECTION,
                    "scriptType": "bash",
                    "scriptLocation": "inlineScript",
                    "inlineScript": "az account show",
                },
            }
        # GCP (and any future provider) -> workload identity federation via a
        # service connection; the provider / service account are referenced by
        # $(var) from the variable group.
        return {
            "task": TASK_AZURE_CLI,
            "displayName": "Authenticate to Google Cloud (OIDC)",
            "inputs": {
                "azureSubscription": GCP_SERVICE_CONNECTION,
                "scriptType": "bash",
                "scriptLocation": "inlineScript",
                "inlineScript": (
                    "gcloud auth login --cred-file=$(GCP_WORKLOAD_IDENTITY_PROVIDER)"
                ),
            },
        }
