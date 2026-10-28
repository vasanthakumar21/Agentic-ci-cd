"""GitLab CI emitter.

Renders the vendor-neutral :class:`StageGraph` into a single GitLab CI file,
``.gitlab-ci.yml``. Like the GitHub and Azure DevOps plugins, this is pure
*orchestration*: it arranges stage order, manual gates, approvals,
timeouts, security scans and cloud OIDC — it never decides what a stage *does*.
The command a stage runs is taken verbatim from ``stage.runs`` (the IR); the
target-platform verbs are composed elsewhere at dispatch time.

 — GitLab CI column mapping
-------------------------------------
 * stage order -> a top-level ``stages:`` list (the authoritative order) plus
 each job's ``stage:`` key. GitLab runs jobs in the order their stage appears
 in the ``stages:`` list, so declared IR order is preserved by the list.
 * manual gate -> ``when: manual`` on the job (a manual stage, or a stage
 whose target environment has ``gate=manual``).
 * approvals -> a *protected environment*: the job references
 ``environment: name``; the required approvals are configured on the GitLab
 protected environment (by the bootstrap step). The pipeline only references
 the environment, it does not (and cannot) embed the approver list.
 * timeout -> ``timeout:`` on the job (``<N> minutes`` from
 ``stage.timeout_minutes``).
 * secrets ref -> ``$NAME`` — masked CI/CD variables, by reference only, never
 values. Role ARNs / client ids come from variable references.
 * cloud OIDC -> ``id_tokens:`` requesting an OIDC JWT with the provider's
 audience, plus a provider-appropriate assume-role / login step (AWS ->
 ``aws sts assume-role-with-web-identity``; Azure -> ``az login
 --federated-token``; GCP -> workload-identity-federation login). All
 identifiers are referenced as ``$VAR`` CI/CD variables.
 * scans -> any stage requesting ``scans`` contributes a generic
 security job per requested scan type (SAST / secret / dependency), placed in
 a dedicated ``security`` stage that heads the ``stages:`` list.

Determinism
------------------
The pipeline must regenerate byte-for-byte. We emit YAML with a small, explicit,
ordered serializer (:mod:`.yaml_emit`) instead of a general YAML dumper whose key
order / style can drift. There are no timestamps and every collection is built in
a fixed, documented order.

``retries`` (GitLab native)
---------------------------
GitLab has a native job ``retry:`` keyword, so a stage's ``retries`` maps cleanly
onto ``retry:`` — no honest-limitation comment needed (unlike GitHub/Azure).
"""

from __future__ import annotations

from cicdgen.ir.models import Cloud, CloudAuth, CloudProvider, GateMode, Pipeline, Stage, WhenMode
from cicdgen.plugins.base import Capability, CIVendorPlugin, GeneratedFile, StageGraph

from .yaml_emit import Comment, YamlValue, dump_mapping

#: Where the pipeline is written, relative to the target repo root.
PIPELINE_PATH = ".gitlab-ci.yml"

#: The runner image used for every job. A fixed, documented default keeps output
#: deterministic; a future IR knob could override it.
DEFAULT_IMAGE = "ubuntu:latest"

#: The name of the synthetic stage that gathers all security scans. It heads the
#: ``stages:`` list so scans run before the ordered build/deploy stages.
SECURITY_STAGE = "security"

#: Generic scan job definitions (kept vendor-neutral / placeholder-ish). The
#: intent is a correct *structure*; the exact scanner is a detail the agent can
#: gap-fill (or a GitLab template include could replace these later).
SCAN_STEP_ACTIONS: dict[str, tuple[str, str]] = {
    # scan type -> (job name, script command)
    "sast": ("sast", "echo 'run SAST scan'"),
    "secrets": ("secret_detection", "echo 'run secret scan'"),
    "dependency": ("dependency_scanning", "echo 'run dependency scan'"),
}

#: CI/CD variable names referenced for cloud OIDC credentials (by reference only).
AWS_ROLE_VAR = "AWS_DEPLOY_ROLE_ARN"
AZURE_CLIENT_ID_VAR = "AZURE_CLIENT_ID"
AZURE_TENANT_ID_VAR = "AZURE_TENANT_ID"
AZURE_SUBSCRIPTION_ID_VAR = "AZURE_SUBSCRIPTION_ID"
GCP_WIF_PROVIDER_VAR = "GCP_WORKLOAD_IDENTITY_PROVIDER"
GCP_SERVICE_ACCOUNT_VAR = "GCP_SERVICE_ACCOUNT"

#: The id_tokens key the job requests; its OIDC audience is provider-appropriate.
ID_TOKEN_NAME = "OIDC_TOKEN"
AWS_OIDC_AUDIENCE = "sts.amazonaws.com"
AZURE_OIDC_AUDIENCE = "api://AzureADTokenExchange"
GCP_OIDC_AUDIENCE = "gitlab"


def _job_name(stage_id: str) -> str:
    """Map a stage id to a GitLab CI job name.

 GitLab job names are free-form strings (they may contain ``-``), but they must
 not collide with GitLab reserved top-level keywords (``stages``, ``variables``,
 ``default``, ``include``, …) and must be non-empty. Stage ids are user-defined;
 we keep the id verbatim when safe and prefix ``job_`` when it is empty or would
 clash with a reserved keyword, deterministically.
 """
    reserved = {
        "stages", "variables", "default", "include", "workflow", "image",
        "services", "cache", "before_script", "after_script", "pages",
    }
    cleaned = stage_id.strip()
    if not cleaned or cleaned in reserved:
        cleaned = f"job_{cleaned}"
    return cleaned


class GitLab(CIVendorPlugin):
    """Emit a ``.gitlab-ci.yml`` from a stage graph.

 ``name`` MUST equal the directory name ``plugins/ci/gitlab`` so the IR value,
 the folder, and this class all agree (see base.py module docstring,).
 """

    name = "gitlab"

    #: Pipeline-expression capabilities GitLab CI can render: protected
    #: environment approvals, ``when: manual`` gates, ID-token OIDC, self-hosted
    #: runners, ``parallel: matrix`` builds, and scan jobs. Secrets stay masked
    #: variable references.
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
        """Render ``.gitlab-ci.yml`` for ``graph``.

 Builds a top-level ``stages:`` list in declared order (with a leading
 ``security`` stage when any scan is requested), one job per *enabled*
 stage keyed by name with its ``stage:``, and generic security jobs when
 scans are asked. Cloud OIDC wires ``id_tokens:`` + an assume-role step
 when ``cloud.auth == oidc``.
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
        """Assemble the whole ``.gitlab-ci.yml`` mapping in a fixed key order."""
        document: dict[str, YamlValue] = {}

        oidc = self._uses_oidc(pipeline.cloud)
        scan_types = self._requested_scans(enabled)

        # Scan-only stages (request scans but have no command) are represented
        # entirely by the synthetic security stage; a stage that also carries a
        # ``runs`` command still gets its own job (its scans are folded in).
        buildable = [s for s in enabled if not self._is_scan_only(s)]

        # The authoritative order: security stage first (when scans requested),
        # then one stage per buildable IR stage in declared order.
        stage_order: list[str] = []
        if scan_types:
            stage_order.append(SECURITY_STAGE)
        for stage in buildable:
            stage_order.append(_job_name(stage.id))
        document["stages"] = stage_order

        # A documented note on where secret values live (masked CI/CD variables).
        document["__comment__"] = Comment(
            "secrets are masked CI/CD variables referenced as $NAME; set them in "
            "the project/group CI/CD settings — values never appear in this file."
        )

        # Security jobs — one per requested scan, all in the security stage.
        if scan_types:
            for scan in scan_types:
                job_name, run_cmd = SCAN_STEP_ACTIONS[scan]
                document[job_name] = self._security_job(job_name, run_cmd)

        # One job per buildable stage, keyed by name, ``stage:`` set for ordering.
        for stage in buildable:
            jname = _job_name(stage.id)
            document[jname] = self._stage_job(
                pipeline=pipeline,
                stage=stage,
                env_gate=env_gate,
                oidc=oidc,
            )

        return document

    # ------------------------------------------------------------------
    # Per-stage job
    # ------------------------------------------------------------------
    def _stage_job(
        self,
        *,
        pipeline: Pipeline,
        stage: Stage,
        env_gate: dict[str, GateMode],
        oidc: bool,
    ) -> dict[str, YamlValue]:
        """Build one GitLab job mapping for a stage (fixed key order)."""
        jname = _job_name(stage.id)
        job: dict[str, YamlValue] = {}
        job["stage"] = jname
        job["image"] = DEFAULT_IMAGE

        # Manual gate OR a manual-gated environment -> ``when: manual``. Approvals
        # are enforced at the (protected) environment level in GitLab.
        manual = stage.when is WhenMode.MANUAL or (
            stage.environment is not None
            and env_gate.get(stage.environment) is GateMode.MANUAL
        )
        if manual:
            job["when"] = "manual"

        # Environment reference (protected env carries the required approvals).
        if stage.environment is not None:
            job["environment"] = {"name": stage.environment}
        elif stage.when is WhenMode.MANUAL:
            # Manual stage without a named environment: still reference an
            # environment so a protected-environment approval can gate it.
            job["environment"] = {"name": stage.id}

        # OIDC: request an id token for the job and assume the cloud role.
        if oidc:
            job["id_tokens"] = {
                ID_TOKEN_NAME: {"aud": self._oidc_audience(pipeline.cloud)}
            }

        if stage.timeout_minutes is not None:
            job["timeout"] = f"{stage.timeout_minutes} minutes"

        if stage.retries and stage.retries > 0:
            # GitLab has a native job retry keyword — map directly (no limitation).
            job["retry"] = stage.retries

        # Script: optional OIDC assume-role step, then the stage command verbatim.
        script: list[YamlValue] = []
        if oidc:
            script.extend(self._cloud_credentials_commands(pipeline.cloud))
        run_cmd = stage.runs if stage.runs is not None else f"echo 'no command for stage {stage.id}'"
        script.append(run_cmd)
        job["script"] = script

        return job

    # ------------------------------------------------------------------
    # Security / scan jobs
    # ------------------------------------------------------------------
    def _is_scan_only(self, stage: Stage) -> bool:
        """True when a stage only requests scans and has no command to run.

 Such a stage is represented entirely by the synthetic security jobs (its
 scans are folded in), so it must not also emit its own build job. A stage
 with both ``scans`` and ``runs`` is NOT scan-only — it still needs its own
 job for the command.
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

    def _security_job(self, job_name: str, run_cmd: str) -> dict[str, YamlValue]:
        """Build one security job (in the security stage) for a requested scan."""
        return {
            "stage": SECURITY_STAGE,
            "image": DEFAULT_IMAGE,
            "script": [run_cmd],
        }

    # ------------------------------------------------------------------
    # Cloud OIDC (ID tokens + assume role)
    # ------------------------------------------------------------------
    def _uses_oidc(self, cloud: Cloud | None) -> bool:
        return cloud is not None and cloud.auth is CloudAuth.OIDC

    def _oidc_audience(self, cloud: Cloud | None) -> str:
        """Provider-appropriate OIDC audience for the requested id token."""
        assert cloud is not None  # only called when _uses_oidc is True
        if cloud.provider is CloudProvider.AWS:
            return AWS_OIDC_AUDIENCE
        if cloud.provider is CloudProvider.AZURE:
            return AZURE_OIDC_AUDIENCE
        return GCP_OIDC_AUDIENCE

    def _cloud_credentials_commands(self, cloud: Cloud | None) -> list[str]:
        """Return the provider-appropriate OIDC assume-role / login commands.

 All identifiers (role ARN, client/tenant/subscription ids, WIF provider)
 are referenced as ``$VAR`` masked CI/CD variables only. The
 id token minted via ``id_tokens:`` is available as ``$OIDC_TOKEN``.
 """
        assert cloud is not None  # only called when _uses_oidc is True
        if cloud.provider is CloudProvider.AWS:
            region = cloud.region if cloud.region else "us-east-1"
            return [
                f"export AWS_DEFAULT_REGION={region}",
                (
                    "aws sts assume-role-with-web-identity "
                    f"--role-arn ${AWS_ROLE_VAR} "
                    f"--role-session-name gitlab-ci "
                    f"--web-identity-token ${ID_TOKEN_NAME}"
                ),
            ]
        if cloud.provider is CloudProvider.AZURE:
            return [
                (
                    "az login --service-principal "
                    f"--username ${AZURE_CLIENT_ID_VAR} "
                    f"--tenant ${AZURE_TENANT_ID_VAR} "
                    f"--federated-token ${ID_TOKEN_NAME}"
                ),
                f"az account set --subscription ${AZURE_SUBSCRIPTION_ID_VAR}",
            ]
        # GCP (and any future provider) -> workload identity federation login.
        return [
            (
                "gcloud iam workload-identity-pools create-cred-config "
                f"${GCP_WIF_PROVIDER_VAR} "
                f"--service-account=${GCP_SERVICE_ACCOUNT_VAR} "
                f"--credential-source-file=${ID_TOKEN_NAME} "
                "--output-file=gcp-creds.json"
            ),
            "gcloud auth login --cred-file=gcp-creds.json",
        ]
