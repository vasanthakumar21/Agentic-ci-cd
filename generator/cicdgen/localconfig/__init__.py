"""Local, git-ignored config (the R6 piece).

Reads ``.cicd/secrets.local.yaml`` (secret VALUES, keyed by name) and
``.cicd/bootstrap.local.yaml`` (non-generatable vendor / cloud WIRING), validates
required keys, cross-checks the secret catalog against the value map, resolves
``*_env`` token references from the environment, and exposes everything through a
clean API.

Hard rule: secret VALUES are never logged, echoed, repr'd, str'd, or
leaked into exceptions. Values are wrapped in Pydantic ``SecretStr`` and are only
retrievable via an explicit ``get_secret_value``-style call. Validation and
cross-check errors name KEYS only, never values.

Public API:

 *:class:`LocalConfig` — the loaded, validated result (wiring + secrets).
 *:func:`load_local_config` — read + validate both files from a ``.cicd`` dir.
 *:class:`SecretStore` — the secret catalog + values, redacting container.
 *:class:`BootstrapConfig` — the parsed ``bootstrap.local.yaml`` wiring.
 *:class:`LocalConfigError` — raised on any missing/invalid required key.
"""

from __future__ import annotations

from cicdgen.localconfig.loader import (
    LocalConfig,
    LocalConfigError,
    load_local_config,
)
from cicdgen.localconfig.models import (
    AwsEcsTarget,
    AwsOidc,
    AzureOidc,
    BootstrapConfig,
    BranchProtection,
    DatabricksTarget,
    OidcConfig,
    RepoConfig,
    RunnerMode,
    RunnersConfig,
    SecretStore,
    TargetConfig,
    VendorAdmin,
)

__all__ = [
    "AwsEcsTarget",
    "AwsOidc",
    "AzureOidc",
    "BootstrapConfig",
    "BranchProtection",
    "DatabricksTarget",
    "LocalConfig",
    "LocalConfigError",
    "OidcConfig",
    "RepoConfig",
    "RunnerMode",
    "RunnersConfig",
    "SecretStore",
    "TargetConfig",
    "VendorAdmin",
    "load_local_config",
]
