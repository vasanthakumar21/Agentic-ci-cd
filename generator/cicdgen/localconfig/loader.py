"""Load + validate the local, git-ignored config from a ``.cicd`` directory.

It:

 1. reads ``bootstrap.local.yaml`` and ``secrets.local.yaml`` from an injectable
 ``.cicd`` directory,
 2. validates both with Pydantic (fail-fast, precise KEY-naming messages),
 3. cross-checks the secret catalog against the value map and treats an empty
 string as MISSING,
 4. resolves ``*_env`` token references (``vendor_admin.token_env``,
 ``runners.registration_token_env``) from :data:`os.environ`, wrapping each
 resolved value in :class:`~pydantic.SecretStr`,
 5. returns a clean :class:`LocalConfig`.

Hard rule: no secret value is ever logged, echoed, repr'd, str'd, or placed into
an exception message. Every error here names KEYS only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from cicdgen.localconfig.models import BootstrapConfig, SecretStore

BOOTSTRAP_FILENAME = "bootstrap.local.yaml"
SECRETS_FILENAME = "secrets.local.yaml"


class LocalConfigError(ValueError):
    """Raised when the local config is missing or fails validation.

 Messages name offending KEYS / files only — never secret values.
 """


class LocalConfig(BaseModel):
    """The loaded, validated local config — wiring (bootstrap) + secrets.

 ``repr``/``str`` are safe to log: the embedded :class:`SecretStore` and the
 resolved ``*_env`` tokens are all redacting :class:`SecretStr` wrappers.
 """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    bootstrap: BootstrapConfig
    secrets: SecretStore
    # Resolved ``*_env`` tokens, keyed by the env-var NAME. Redacting wrappers.
    env_tokens: dict[str, SecretStr]

    # --- clean read API ----------------------------------------------------
    def get_secret(self, name: str) -> str:
        """Reveal the raw value of a catalogued secret — the explicit opt-in call.

 Never log the return value.
 """
        return self.secrets.get_secret_value(name)

    def secret_names(self) -> list[str]:
        """The catalogued secret names (keys only — safe to log)."""
        return self.secrets.names()

    def get_env_token(self, env_var_name: str) -> str:
        """Reveal a resolved ``*_env`` token value by its env-var name (opt-in)."""
        if env_var_name not in self.env_tokens:
            raise KeyError(f"no resolved env token for '{env_var_name}'.")
        return self.env_tokens[env_var_name].get_secret_value()

    def required_keys(self) -> dict[str, list[str]]:
        """Describe what was required/loaded, by KEY only (safe to log).

 Returns a map with the secret catalog and the env-var names that were
 resolved — useful for a dry-run/bootstrap summary without touching values.
 """
        return {
            "secrets": self.secret_names(),
            "env_tokens": sorted(self.env_tokens),
        }

    def __repr__(self) -> str:  # keys only — never values
        return (
            "LocalConfig("
            f"repo={self.bootstrap.repo.owner}/{self.bootstrap.repo.name}, "
            f"secrets={self.secret_names()}, "
            f"env_tokens={sorted(self.env_tokens)})"
        )

    __str__ = __repr__


def _read_yaml_mapping(path: Path, *, allow_empty: bool) -> dict[str, Any]:
    if not path.is_file():
        raise LocalConfigError(f"local config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LocalConfigError(f"{path.name} is not valid YAML ({path}): {exc}") from exc
    if data is None:
        if allow_empty:
            return {}
        raise LocalConfigError(f"{path.name} is empty ({path}); expected a YAML mapping.")
    if not isinstance(data, dict):
        raise LocalConfigError(
            f"{path.name} must contain a YAML mapping at the top level ({path}); "
            f"got {type(data).__name__}."
        )
    return data


def _format_validation_error(filename: str, exc: ValidationError) -> str:
    # Pydantic includes the offending input in error context; we deliberately build
    # the message from loc + msg only so no value is echoed.
    lines = [f"Invalid {filename} — {exc.error_count()} error(s):"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def _resolve_env_tokens(
    bootstrap: BootstrapConfig,
    environ: dict[str, str],
) -> dict[str, SecretStr]:
    """Resolve the ``*_env`` references from the given environment mapping.

 Required: ``vendor_admin.token_env``. Required only for self-hosted runners:
 ``runners.registration_token_env``. A referenced-but-unset env var fails
 naming the VARIABLE (never its value).
 """
    wanted: list[str] = [bootstrap.vendor_admin.token_env]
    if bootstrap.runners.registration_token_env:
        wanted.append(bootstrap.runners.registration_token_env)

    resolved: dict[str, SecretStr] = {}
    missing: list[str] = []
    for var in wanted:
        value = environ.get(var)
        if value is None or value.strip() == "":
            missing.append(var)
            continue
        resolved[var] = SecretStr(value)

    if missing:
        raise LocalConfigError(
            "required token environment variable(s) not set (or empty): "
            f"{sorted(missing)}. Export them before running "
            "(e.g. `export CICD_ADMIN_TOKEN=...`)."
        )
    return resolved


def load_local_config(
    cicd_dir: Path | str,
    *,
    resolve_env_tokens: bool = True,
    environ: dict[str, str] | None = None,
) -> LocalConfig:
    """Load + validate both local config files from a ``.cicd`` directory.

 Args:
 cicd_dir: the injectable directory holding ``bootstrap.local.yaml`` and
 ``secrets.local.yaml``.
 resolve_env_tokens: when True (default), resolve ``*_env`` references from
 the environment and fail if a required one is unset. Set False to
 validate shape/catalog without requiring the tokens to be exported
 (useful for a pure shape check).
 environ: inject an environment mapping for testing; defaults to
:data:`os.environ`.

 Returns:
 A frozen :class:`LocalConfig`. Its ``repr`` is safe to log.

 Raises:
 LocalConfigError: on a missing file, bad YAML, a shape/enum/required-key
 error, a catalog/value cross-check failure, or an unset required
 token env var — always naming KEYS only, never values.
 """
    base = Path(cicd_dir)

    bootstrap_raw = _read_yaml_mapping(base / BOOTSTRAP_FILENAME, allow_empty=False)
    secrets_raw = _read_yaml_mapping(base / SECRETS_FILENAME, allow_empty=True)

    try:
        bootstrap = BootstrapConfig.model_validate(bootstrap_raw)
    except ValidationError as exc:
        raise LocalConfigError(_format_validation_error(BOOTSTRAP_FILENAME, exc)) from exc

    try:
        store = SecretStore.from_name_value_map(bootstrap.secrets, secrets_raw)
    except ValueError as exc:
        # ValueError messages from the cross-check name KEYS only.
        raise LocalConfigError(str(exc)) from exc

    if resolve_env_tokens:
        env_source = environ if environ is not None else dict(os.environ)
        env_tokens = _resolve_env_tokens(bootstrap, env_source)
    else:
        env_tokens = {}

    return LocalConfig(bootstrap=bootstrap, secrets=store, env_tokens=env_tokens)
