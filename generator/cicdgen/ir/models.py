"""Pydantic models for the IR (``pipeline.yaml``).

Fail-fast validation: unknown fields, bad enums, or missing required keys fail
with a precise, field-level message. The models here are the Python mirror of
``ir/pipeline.schema.json`` — the JSON Schema is the published contract; these
models are the executable validator the generator core actually runs.

Every optional knob carries its documented default:

 * stage.enabled -> True
 * stage.retries -> 0
 * stage.when -> auto
 * environment.gate -> auto
 * environment.protection -> none
 * cloud.auth -> oidc

Cross-field rules the flat schema cannot express live in ``model_validator``s:

 * a stage's ``environment`` must name a declared environment
 * stage ids must be unique
 * environment names must be unique

The ci_vendor / target_platform *plugin existence* check is deliberately kept
out of these models — it is a separate validation layer (``plugins.py``) so the
pure IR shape can be validated and tested independently of the plugins.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Enums — kept in lock-step with the JSON Schema enums
# ---------------------------------------------------------------------------
class CiVendor(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"
    AZURE_DEVOPS = "azure-devops"
    BITBUCKET = "bitbucket"
    JENKINS = "jenkins"


class TargetPlatform(StrEnum):
    AWS_ECS = "aws-ecs"
    AWS_LAMBDA = "aws-lambda"
    AWS_EKS = "aws-eks"
    AWS_GLUE = "aws-glue"
    AWS_EMR = "aws-emr"
    AWS_STEPFUNCTIONS = "aws-stepfunctions"
    AWS_BATCH = "aws-batch"
    AWS_SAGEMAKER = "aws-sagemaker"
    DATABRICKS = "databricks"
    AZURE_FUNCTIONS = "azure-functions"
    AZURE_AKS = "azure-aks"
    AZURE_SYNAPSE = "azure-synapse"
    AZURE_DATA_FACTORY = "azure-data-factory"
    AZURE_CONTAINER_APPS = "azure-container-apps"
    KUBERNETES = "kubernetes"
    GCP_CLOUD_RUN = "gcp-cloud-run"
    GCP_GKE = "gcp-gke"
    GCP_DATAPROC = "gcp-dataproc"
    GCP_DATAFLOW = "gcp-dataflow"


class CloudProvider(StrEnum):
    AWS = "aws"
    AZURE = "azure"
    GCP = "gcp"


class CloudAuth(StrEnum):
    OIDC = "oidc"
    KEYS = "keys"


class GateMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class Protection(StrEnum):
    NONE = "none"
    REQUIRED_APPROVALS = "required-approvals"


class WhenMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class ScanType(StrEnum):
    SAST = "sast"
    SECRETS = "secrets"
    DEPENDENCY = "dependency"


# ``extra="forbid"`` is the fail-fast switch: an unknown field raises a precise
# error naming the offending key, exactly like additionalProperties:false in the
# JSON Schema.
_STRICT = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Leaf / nested models
# ---------------------------------------------------------------------------
class Project(BaseModel):
    """Project identity."""

    model_config = _STRICT

    name: str = Field(..., min_length=1)
    language: str | None = Field(default=None, min_length=1)


class Cloud(BaseModel):
    """Cloud context, driven by target_platform. ``auth`` defaults to oidc."""

    model_config = _STRICT

    provider: CloudProvider
    region: str | None = Field(default=None, min_length=1)
    auth: CloudAuth = CloudAuth.OIDC


class Environment(BaseModel):
    """A deployment environment and its gate/protection rules.

 ``gate`` defaults to auto and ``protection`` to none.
 """

    model_config = _STRICT

    name: str = Field(..., min_length=1)
    gate: GateMode = GateMode.AUTO
    protection: Protection = Protection.NONE


class Approvals(BaseModel):
    """Approval requirements before a stage proceeds."""

    model_config = _STRICT

    required: int = Field(..., ge=1)
    from_group: str | None = Field(default=None, min_length=1)


class Stage(BaseModel):
    """A single ordered pipeline stage.

 ``enabled`` defaults to True, ``retries`` to 0, ``when`` to auto.

 ``depends_on`` is an optional list of stage ids this stage must run after.
 When omitted/empty the declared list order remains the execution order
 exactly; ``depends_on`` only *adds* ordering constraints, it never silently
 reorders a pipeline that does not use it. That every id names a declared stage
 and that the edges form a DAG are cross-stage rules enforced in
 ``cicdgen.core.stage_graph``.
 """

    model_config = _STRICT

    id: str = Field(..., min_length=1)
    enabled: bool = True
    timeout_minutes: int | None = Field(default=None, ge=1)
    retries: int = Field(default=0, ge=0)
    runs: str | None = Field(default=None, min_length=1)
    scans: list[ScanType] | None = None
    environment: str | None = Field(default=None, min_length=1)
    when: WhenMode = WhenMode.AUTO
    approvals: Approvals | None = None
    depends_on: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def depends_on_unique_and_nonempty(self) -> Stage:
        """Mirror the schema's minLength/uniqueItems on depends_on precisely.

 Each entry must be a non-empty stage id and the list must not repeat an
 id (a stage declaring the same dependency twice is a spec error). Whether
 each id *names a declared stage* and whether the edges form a DAG are
 cross-stage concerns resolved in ``cicdgen.core.stage_graph``, not here —
 this validator stays per-stage like ``scans_unique``.
 """
        for dep in self.depends_on:
            if not dep:
                raise ValueError(
                    f"stage '{self.id}': depends_on entries must be non-empty stage ids."
                )
        if len(set(self.depends_on)) != len(self.depends_on):
            dupes = {d for d in self.depends_on if self.depends_on.count(d) > 1}
            raise ValueError(
                f"stage '{self.id}': duplicate depends_on id(s) {sorted(dupes)} — "
                f"each dependency may be listed at most once."
            )
        return self

    @model_validator(mode="after")
    def scans_unique(self) -> Stage:
        """Mirror the schema's uniqueItems on scans with a precise message."""
        if self.scans is not None:
            seen = [s.value for s in self.scans]
            dupes = {s for s in seen if seen.count(s) > 1}
            if dupes:
                raise ValueError(
                    f"stage '{self.id}': duplicate scan(s) {sorted(dupes)} — "
                    f"each scan type may appear at most once."
                )
        return self


class Thresholds(BaseModel):
    """Quality/performance gates."""

    model_config = _STRICT

    coverage_min_percent: float | None = Field(default=None, ge=0, le=100)
    max_build_minutes: int | None = Field(default=None, ge=1)


class Notifications(BaseModel):
    """Notification configuration."""

    model_config = _STRICT

    recipients: list[str] | None = None

    @model_validator(mode="after")
    def recipients_unique_and_nonempty(self) -> Notifications:
        if self.recipients is not None:
            for r in self.recipients:
                if not r:
                    raise ValueError("notifications.recipients entries must be non-empty.")
            if len(set(self.recipients)) != len(self.recipients):
                raise ValueError("notifications.recipients must not contain duplicates.")
        return self


class Triggers(BaseModel):
    """Which branches start the pipeline.

 OPTIONAL. When absent the generator uses its documented default — push +
 pull_request on ``main``. When present it lets a team follow a branch
 strategy such as **branch-per-ticket**: e.g.
 ``push: ["main", "feature/**", "TEK-*"]`` so every ticket branch runs its own
 pipeline and changes are monitored per ticket.

 Both lists are branch *patterns* in the vendor's glob syntax (``*`` matches
 within a path segment, ``**`` across segments). ``push_branches`` drives the
 push trigger; ``pr_branches`` drives the pull_request trigger (the branches a
 PR may TARGET). Either may be omitted to keep that trigger's default.
 """

    model_config = _STRICT

    push_branches: list[str] = Field(default_factory=lambda: ["main"], min_length=1)
    pr_branches: list[str] = Field(default_factory=lambda: ["main"], min_length=1)

    @model_validator(mode="after")
    def branches_nonempty(self) -> Triggers:
        for field_name, values in (("push_branches", self.push_branches), ("pr_branches", self.pr_branches)):
            for b in values:
                if not b or not b.strip():
                    raise ValueError(f"triggers.{field_name} entries must be non-empty branch patterns.")
        return self


class Promotion(BaseModel):
    """Build-once / promote-across-environments configuration.

 OPTIONAL. When absent (``pipeline.promotion is None``) the generator composes
 verbs per stage. When present with ``enabled: true`` the core shapes the
 composition so ONE ``build``+``package`` pair produces an immutable artifact
 reference and each environment named in ``path`` runs only ``deploy``+``verify``
 consuming the SAME reference (no environment re-runs build/package); the chosen
 ``target_platform`` plugin must declare ``Capability.ARTIFACT_PROMOTION`` or the
 capability gate fails fast.

 ``path`` accepts either a list of environment names in promote order, or a
 human-friendly ``"dev -> staging -> prod"`` string which is normalised to a
 list. Every name in ``path`` MUST be a declared environment — a cross-field
 rule enforced on :class:`Pipeline` (``promotion_path_must_be_declared``),
 fail-fast like ``stage_environment_must_be_declared``.
 """

    model_config = _STRICT

    enabled: bool = True
    artifact: str = Field(..., min_length=1)
    path: list[str] = Field(..., min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _normalise_path(cls, data: object) -> object:
        """Accept a ``"a -> b -> c"`` string for ``path`` and split it to a list.

 The arrow-string form is normalised to the list form here so the rest of
 the model — and every downstream consumer — only
 ever sees ``path`` as an ordered ``list[str]``. A value that is already a
 list is passed through untouched.
 """
        if isinstance(data, dict):
            raw = data.get("path")
            if isinstance(raw, str):
                parts = [segment.strip() for segment in raw.split("->")]
                data = {**data, "path": parts}
        return data

    @model_validator(mode="after")
    def path_entries_valid(self) -> Promotion:
        """Each promote-path entry must be a non-empty, non-repeated environment id.

 Whether each name *is a declared environment* is a cross-field concern
 resolved on :class:`Pipeline` (``promotion_path_must_be_declared``); this
 per-model validator only enforces that the list is well-formed (mirrors
 ``depends_on_unique_and_nonempty`` on :class:`Stage`).
 """
        for entry in self.path:
            if not entry:
                raise ValueError(
                    "promotion.path entries must be non-empty environment names "
                    "(check for an empty segment in a 'a -> b -> c' path)."
                )
        if len(set(self.path)) != len(self.path):
            dupes = {e for e in self.path if self.path.count(e) > 1}
            raise ValueError(
                f"promotion.path contains duplicate environment(s): {sorted(dupes)}. "
                f"Each environment may appear at most once in the promote order."
            )
        return self


# ---------------------------------------------------------------------------
# Root model
# ---------------------------------------------------------------------------
class Pipeline(BaseModel):
    """Root IR model — the parsed, defaults-resolved ``pipeline.yaml``.

 Validating this model performs: unknown-field rejection, enum checks,
 required-key checks (all fail-fast with field paths), default resolution,
 and the cross-field rules below. The ci_vendor / target_platform *plugin
 existence* check is a separate layer (``plugins.py``).
 """

    model_config = _STRICT

    version: int = Field(..., description="IR schema version; only 1 is supported.")
    ci_vendor: CiVendor
    target_platform: TargetPlatform
    project: Project
    cloud: Cloud | None = None
    environments: list[Environment] = Field(default_factory=list)
    stages: list[Stage] = Field(..., min_length=1)
    thresholds: Thresholds | None = None
    notifications: Notifications | None = None
    promotion: Promotion | None = None
    triggers: Triggers | None = None

    @model_validator(mode="after")
    def version_is_supported(self) -> Pipeline:
        if self.version != 1:
            raise ValueError(f"version must be 1 (the only supported IR version). Got: {self.version}.")
        return self

    @model_validator(mode="after")
    def environment_names_unique(self) -> Pipeline:
        names = [e.name for e in self.environments]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(
                f"environments contains duplicate name(s): {sorted(dupes)}. "
                f"Each environment name must be unique."
            )
        return self

    @model_validator(mode="after")
    def stage_ids_unique(self) -> Pipeline:
        ids = [s.id for s in self.stages]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(
                f"stages contains duplicate id(s): {sorted(dupes)}. Each stage id must be unique."
            )
        return self

    @model_validator(mode="after")
    def stage_environment_must_be_declared(self) -> Pipeline:
        """A stage's ``environment`` must name a declared environment (cross-field).

 The flat JSON Schema cannot express this relationship; it is enforced
 here so a typo surfaces at validation time, not during generation.
 """
        declared = {e.name for e in self.environments}
        for stage in self.stages:
            if stage.environment is not None and stage.environment not in declared:
                known = sorted(declared) if declared else "(none declared)"
                raise ValueError(
                    f"stage '{stage.id}' targets environment '{stage.environment}', "
                    f"which is not declared under 'environments'. Declared environments: {known}."
                )
        return self

    @model_validator(mode="after")
    def promotion_path_must_be_declared(self) -> Pipeline:
        """Every environment in ``promotion.path`` must name a declared environment.

 Cross-field and fail-fast, in the same style as
 ``stage_environment_must_be_declared``: a typo in the promote path surfaces
 at validation time, not during generation. When ``promotion`` is absent
 this is a no-op.
 """
        if self.promotion is None:
            return self
        declared = {e.name for e in self.environments}
        for env_name in self.promotion.path:
            if env_name not in declared:
                known = sorted(declared) if declared else "(none declared)"
                raise ValueError(
                    f"promotion.path references environment '{env_name}', which is "
                    f"not declared under 'environments'. Declared environments: {known}."
                )
        return self
