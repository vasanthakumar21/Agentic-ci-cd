"""Pydantic models for the local, git-ignored config (the R6 piece).

These mirror the shapes documented in the committed ``.cicd/*.example`` files
(authored in) and follow the same fail-fast Pydantic-v2 conventions as
the IR models (``cicdgen.ir.models``): ``extra="forbid"`` so an unknown key fails
with a precise, key-naming message.

``bootstrap.local.yaml`` describes the non-generatable WIRING; it holds NO secret
values — only secret NAMES (the catalog) and ``*_env`` references the loader reads
from the environment. Optional axis blocks (``oidc.azure``, ``target.databricks``,
…) may be omitted entirely; the models are tolerant of that by design.

Secret VALUES live in ``secrets.local.yaml`` and are wrapped in
:class:`~pydantic.SecretStr` by :class:`SecretStore` so they never appear in
``repr``/``str``/logs/exceptions. A value is retrievable only via an
explicit ``get_secret_value`` call.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

# ``extra="forbid"`` is the fail-fast switch: an unknown key raises a precise
# error naming the offending field, exactly like the IR models.
_STRICT = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# repo — REQUIRED, all vendors
# ---------------------------------------------------------------------------
class RepoConfig(BaseModel):
    """Where the generated pipeline + IaC live."""

    model_config = _STRICT

    owner: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    create_if_missing: bool = False
    default_branch: str = Field(default="main", min_length=1)
    host: str | None = Field(default=None, min_length=1)


# ---------------------------------------------------------------------------
# vendor_admin — REQUIRED. Token VALUE is read from env by name, never stored.
# ---------------------------------------------------------------------------
class VendorAdmin(BaseModel):
    """The one-time admin credential reference the bootstrapper uses.

 ``token_env`` is the NAME of an environment variable; the value is resolved
 by the loader at load time and exposed as a :class:`SecretStr` — never stored
 in the file and never logged.
 """

    model_config = _STRICT

    token_env: str = Field(..., min_length=1)
    api_url: str | None = Field(default=None, min_length=1)


# ---------------------------------------------------------------------------
# oidc — per-cloud keyless auth; both sub-blocks optional (fill what target uses)
# ---------------------------------------------------------------------------
class AwsOidc(BaseModel):
    """AWS OIDC trust details."""

    model_config = _STRICT

    account_id: str = Field(..., min_length=1)
    role_name: str = Field(..., min_length=1)
    region: str = Field(..., min_length=1)
    thumbprint: str = Field(default="auto", min_length=1)
    provider_arn: str | None = Field(default=None, min_length=1)


class AzureOidc(BaseModel):
    """Azure workload-identity-federation details."""

    model_config = _STRICT

    tenant_id: str = Field(..., min_length=1)
    subscription_id: str = Field(..., min_length=1)
    client_id: str = Field(..., min_length=1)
    resource_group: str | None = Field(default=None, min_length=1)


class OidcConfig(BaseModel):
    """Cloud OIDC wiring. Both sub-blocks are optional; a cross-cloud setup may
 fill both. Omitting the block entirely is valid for key-based or no-cloud axes.
 """

    model_config = _STRICT

    aws: AwsOidc | None = None
    azure: AzureOidc | None = None


# ---------------------------------------------------------------------------
# runners — REQUIRED, all vendors
# ---------------------------------------------------------------------------
class RunnerMode(StrEnum):
    SHARED = "shared"
    SELF_HOSTED = "self-hosted"


class RunnersConfig(BaseModel):
    """Where pipeline jobs execute.

 ``registration_token_env`` is REQUIRED only when ``mode == self-hosted`` —
 enforced below so the missing key fails naming itself. As with the
 admin token, only the env-var NAME lives here; the value is resolved from the
 environment and wrapped as a secret.
 """

    model_config = _STRICT

    mode: RunnerMode = RunnerMode.SHARED
    registration_token_env: str | None = Field(default=None, min_length=1)
    labels: list[str] | None = None

    @model_validator(mode="after")
    def self_hosted_requires_token_env(self) -> RunnersConfig:
        if self.mode is RunnerMode.SELF_HOSTED and not self.registration_token_env:
            raise ValueError(
                "runners.registration_token_env is required when runners.mode is "
                "'self-hosted' (name of the env var holding the runner registration token)."
            )
        return self


# ---------------------------------------------------------------------------
# branch_protection — keyed by branch name
# ---------------------------------------------------------------------------
class BranchProtection(BaseModel):
    """Merge rules for one branch."""

    model_config = _STRICT

    required_approvals: int = Field(..., ge=0)
    required_checks: list[str] = Field(default_factory=list)
    enforce_admins: bool = False
    dismiss_stale_reviews: bool = True


# ---------------------------------------------------------------------------
# target — per-target-platform wiring; every block optional (fill the axis used)
# ---------------------------------------------------------------------------
class AwsEcsTarget(BaseModel):
    """aws-ecs wiring: image → ECR → ECS service."""

    model_config = _STRICT

    ecr_repo_secret: str = Field(..., min_length=1)
    cluster: str = Field(..., min_length=1)
    service: str = Field(..., min_length=1)
    task_family: str = Field(..., min_length=1)
    container_port: int | None = Field(default=None, ge=1, le=65535)


class DatabricksTarget(BaseModel):
    """databricks wiring: notebooks/wheel → job (NO image)."""

    model_config = _STRICT

    host_secret: str = Field(..., min_length=1)
    token_secret: str = Field(..., min_length=1)
    job_name: str = Field(..., min_length=1)
    artifact_path: str | None = Field(default=None, min_length=1)
    unity_catalog: str | None = Field(default=None, min_length=1)


class TargetConfig(BaseModel):
    """Per-target-platform wiring. Every block is optional so the file supports any
 ``target_platform`` the IR declares; only the chosen block need be present.

 ``populate_by_name`` lets the ``aws-ecs`` YAML key (hyphenated, matching the
 ``target_platform`` enum value) map to the ``aws_ecs`` field.
 """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    aws_ecs: AwsEcsTarget | None = Field(default=None, alias="aws-ecs")
    databricks: DatabricksTarget | None = None


# ---------------------------------------------------------------------------
# bootstrap.local.yaml root — the non-generatable wiring
# ---------------------------------------------------------------------------
class BootstrapConfig(BaseModel):
    """Parsed ``bootstrap.local.yaml`` (no secret values — names + wiring only).

 Optional axis blocks (``oidc``, ``reviewers``, ``branch_protection``,
 ``target``) may be omitted; the required spine is ``repo``, ``vendor_admin``,
 ``runners``, and the ``secrets`` catalog.
 """

    model_config = _STRICT

    repo: RepoConfig
    vendor_admin: VendorAdmin
    runners: RunnersConfig
    oidc: OidcConfig | None = None
    reviewers: dict[str, list[str]] = Field(default_factory=dict)
    branch_protection: dict[str, BranchProtection] = Field(default_factory=dict)
    target: TargetConfig | None = None
    secrets: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def secret_catalog_is_sane(self) -> BootstrapConfig:
        """Catalog names must be non-empty and unique (names only, never values)."""
        for name in self.secrets:
            if not name or not name.strip():
                raise ValueError("secrets catalog contains an empty/blank secret name.")
        dupes = sorted({n for n in self.secrets if self.secrets.count(n) > 1})
        if dupes:
            raise ValueError(
                f"secrets catalog contains duplicate name(s): {dupes}. "
                f"Each secret name must appear once."
            )
        return self


# ---------------------------------------------------------------------------
# SecretStore — the secret catalog + redacting value container
# ---------------------------------------------------------------------------
class SecretStore(BaseModel):
    """Holds secret VALUES wrapped in :class:`SecretStr`, keyed by name.

 Pydantic's ``SecretStr`` is the secret-wrapping type: its ``repr``/``str``
 render as ``**********`` and the real value is retrievable only via an explicit
 ``.get_secret_value`` call. This container's own ``repr``/``str`` therefore
 expose only KEYS (secret names), never values — so the store is safe to log.

 Build instances via :meth:`from_name_value_map`, which cross-checks the catalog
 against the value map and treats an empty string as a MISSING value.
 """

    model_config = ConfigDict(frozen=True)

    values: dict[str, SecretStr] = Field(default_factory=dict)

    def get(self, name: str) -> SecretStr:
        """Return the wrapped secret for ``name`` (still redacted on repr/str).

 Raises:
 KeyError: if the name is not present (message names the KEY only).
 """
        if name not in self.values:
            raise KeyError(f"no secret named '{name}' is loaded.")
        return self.values[name]

    def get_secret_value(self, name: str) -> str:
        """Explicitly reveal the raw value for ``name`` — the ONLY way to do so.

 Callers must opt in by name; the value returned here must never be logged.
 """
        return self.get(name).get_secret_value()

    def names(self) -> list[str]:
        """Sorted list of loaded secret names (keys only — safe to log)."""
        return sorted(self.values)

    def __contains__(self, name: object) -> bool:
        return name in self.values

    def __repr__(self) -> str:  # keys only — never values
        return f"SecretStore(names={self.names()})"

    __str__ = __repr__

    @classmethod
    def from_name_value_map(
        cls,
        catalog: list[str],
        raw_values: dict[str, object],
    ) -> SecretStore:
        """Cross-check the catalog against the raw value map and wrap values.

 Rules enforced (errors name KEYS only, never values):
 * every name in ``catalog`` must have a value in ``raw_values``;
 * an empty string ``""`` is treated as MISSING;
 * values present in ``raw_values`` but absent from the catalog are
 reported (so a stray/renamed secret is not silently ignored).

 Args:
 catalog: the secret NAMES declared in ``bootstrap.local.yaml``.
 raw_values: the flat ``NAME: value`` map from ``secrets.local.yaml``.

 Raises:
 ValueError: naming the missing / empty / uncatalogued KEYS.
 """
        catalog_set = set(catalog)
        value_keys = set(raw_values)

        missing: list[str] = []
        for name in catalog:
            if name not in raw_values:
                missing.append(name)
                continue
            value = raw_values[name]
            if value is None or (isinstance(value, str) and value.strip() == ""):
                missing.append(name)

        if missing:
            raise ValueError(
                "secret value(s) missing or empty in secrets.local.yaml for "
                f"catalogued name(s): {sorted(missing)}. "
                "Provide a non-empty value for each (an empty string is treated as missing)."
            )

        uncatalogued = sorted(value_keys - catalog_set)
        if uncatalogued:
            raise ValueError(
                "secrets.local.yaml defines value(s) not listed in the "
                f"bootstrap.local.yaml 'secrets:' catalog: {uncatalogued}. "
                "Add them to the catalog or remove the stray value(s)."
            )

        wrapped = {name: SecretStr(str(raw_values[name])) for name in catalog}
        return cls(values=wrapped)
