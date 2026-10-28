"""Unit tests for the local-config loader (task 4.3).

Covers the fail-fast + never-leak contract of ``cicdgen.localconfig`` (R6, R9.3):

    * happy path — a valid pair of ``bootstrap.local.yaml`` / ``secrets.local.yaml``
      loads; catalogued secrets and ``*_env`` tokens resolve to their real values;
    * a missing REQUIRED top-level key (e.g. ``vendor_admin`` / ``repo``) fails with
      a ``LocalConfigError`` naming the key;
    * a missing / empty secret VALUE for a catalogued name fails naming that key —
      an empty string is treated as missing (R6.5);
    * a stray, uncatalogued value in ``secrets.local.yaml`` fails naming it;
    * ``self-hosted`` runners without ``registration_token_env`` fail naming the key;
    * a referenced-but-unset required token env var fails naming the env var;
    * CRITICAL (R9.3 / R6.6): a real secret VALUE never appears in ``str``/``repr``
      of the config or store, nor in the text of ANY raised ``LocalConfigError`` —
      while the explicit reveal calls still return the true value;
    * ``extra="forbid"``: an unknown key is rejected naming the offending field.

All fixtures write real files under ``tmp_path`` and inject an ``environ`` dict, so
the tests never touch the developer's real ``.cicd`` dir or process environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cicdgen.localconfig import (
    BootstrapConfig,
    LocalConfig,
    LocalConfigError,
    SecretStore,
    load_local_config,
)

# A real-looking secret value used to prove it NEVER leaks into logs/repr/errors.
SECRET_SENTINEL = "s3cr3t-ECR-value-SHOULD-NEVER-LEAK-42"
TOKEN_SENTINEL = "ghp_adminTOKEN_SHOULD_NEVER_LEAK_99"
ADMIN_ENV = "CICD_ADMIN_TOKEN"
RUNNER_ENV = "RUNNER_TOKEN"


# ---------------------------------------------------------------------------
# helpers — build valid-by-default bootstrap / secrets dicts, then mutate
# ---------------------------------------------------------------------------
def _valid_bootstrap() -> dict:
    """A minimal-but-complete, valid bootstrap mapping (shared runners)."""
    return {
        "repo": {"owner": "my-org", "name": "my-service"},
        "vendor_admin": {"token_env": ADMIN_ENV},
        "runners": {"mode": "shared"},
        "target": {
            "aws-ecs": {
                "ecr_repo_secret": "ECR_REPO",
                "cluster": "my-cluster",
                "service": "my-service",
                "task_family": "my-service-task",
            }
        },
        "secrets": ["ECR_REPO"],
    }


def _valid_secrets() -> dict:
    return {"ECR_REPO": SECRET_SENTINEL}


def _write_cicd(
    cicd_dir: Path,
    bootstrap: dict | None,
    secrets: dict | None,
) -> Path:
    """Write bootstrap/secrets YAML into ``cicd_dir`` and return the dir.

    Passing ``None`` skips writing that file (used to test a missing file).
    """
    cicd_dir.mkdir(parents=True, exist_ok=True)
    if bootstrap is not None:
        (cicd_dir / "bootstrap.local.yaml").write_text(
            yaml.safe_dump(bootstrap), encoding="utf-8"
        )
    if secrets is not None:
        (cicd_dir / "secrets.local.yaml").write_text(
            yaml.safe_dump(secrets), encoding="utf-8"
        )
    return cicd_dir


@pytest.fixture
def cicd(tmp_path: Path) -> Path:
    """A ``.cicd`` dir pre-populated with a valid bootstrap + secrets pair."""
    return _write_cicd(tmp_path / ".cicd", _valid_bootstrap(), _valid_secrets())


def _env(**extra: str) -> dict[str, str]:
    """A minimal environment with the admin token set; override via kwargs."""
    base = {ADMIN_ENV: TOKEN_SENTINEL}
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# 1. happy path
# ---------------------------------------------------------------------------
def test_happy_path_loads_and_resolves(cicd: Path) -> None:
    config = load_local_config(cicd, environ=_env())

    assert isinstance(config, LocalConfig)
    assert config.bootstrap.repo.owner == "my-org"
    assert config.secret_names() == ["ECR_REPO"]

    # Explicit reveal returns the REAL values.
    assert config.get_secret("ECR_REPO") == SECRET_SENTINEL
    assert config.get_env_token(ADMIN_ENV) == TOKEN_SENTINEL

    # required_keys() exposes KEYS only (safe to log).
    assert config.required_keys() == {
        "secrets": ["ECR_REPO"],
        "env_tokens": [ADMIN_ENV],
    }


def test_resolve_env_tokens_false_skips_token_requirement(cicd: Path) -> None:
    # Shape/catalog check without exporting the admin token.
    config = load_local_config(cicd, resolve_env_tokens=False, environ={})
    assert config.get_secret("ECR_REPO") == SECRET_SENTINEL
    assert config.required_keys()["env_tokens"] == []


# ---------------------------------------------------------------------------
# 2. missing required top-level key
# ---------------------------------------------------------------------------
def test_missing_vendor_admin_key_names_the_key(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    del bootstrap["vendor_admin"]
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    with pytest.raises(LocalConfigError) as exc:
        load_local_config(cicd, environ=_env())
    assert "vendor_admin" in str(exc.value)


def test_missing_repo_key_names_the_key(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    del bootstrap["repo"]
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    with pytest.raises(LocalConfigError) as exc:
        load_local_config(cicd, environ=_env())
    assert "repo" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. missing / empty secret value for a catalogued name
# ---------------------------------------------------------------------------
def test_missing_secret_value_names_the_key(tmp_path: Path) -> None:
    cicd = _write_cicd(tmp_path / ".cicd", _valid_bootstrap(), {})  # no values

    with pytest.raises(LocalConfigError) as exc:
        load_local_config(cicd, environ=_env())
    assert "ECR_REPO" in str(exc.value)


def test_empty_string_secret_value_is_treated_as_missing(tmp_path: Path) -> None:
    cicd = _write_cicd(tmp_path / ".cicd", _valid_bootstrap(), {"ECR_REPO": ""})

    with pytest.raises(LocalConfigError) as exc:
        load_local_config(cicd, environ=_env())
    assert "ECR_REPO" in str(exc.value)


# ---------------------------------------------------------------------------
# 4. stray / uncatalogued value in secrets.local.yaml
# ---------------------------------------------------------------------------
def test_uncatalogued_stray_secret_value_is_named(tmp_path: Path) -> None:
    secrets = {"ECR_REPO": SECRET_SENTINEL, "STRAY_SECRET": "whatever"}
    cicd = _write_cicd(tmp_path / ".cicd", _valid_bootstrap(), secrets)

    with pytest.raises(LocalConfigError) as exc:
        load_local_config(cicd, environ=_env())
    assert "STRAY_SECRET" in str(exc.value)


# ---------------------------------------------------------------------------
# 5. self-hosted runners without registration_token_env
# ---------------------------------------------------------------------------
def test_self_hosted_runners_require_registration_token_env(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["runners"] = {"mode": "self-hosted"}  # missing registration_token_env
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    with pytest.raises(LocalConfigError) as exc:
        load_local_config(cicd, environ=_env())
    assert "registration_token_env" in str(exc.value)


def test_self_hosted_runners_with_token_env_loads(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["runners"] = {
        "mode": "self-hosted",
        "registration_token_env": RUNNER_ENV,
    }
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    config = load_local_config(cicd, environ=_env(**{RUNNER_ENV: "runner-reg-token"}))
    assert config.get_env_token(RUNNER_ENV) == "runner-reg-token"
    assert sorted(config.required_keys()["env_tokens"]) == sorted([ADMIN_ENV, RUNNER_ENV])


# ---------------------------------------------------------------------------
# 6. required token env var unset
# ---------------------------------------------------------------------------
def test_unset_required_token_env_var_is_named(cicd: Path) -> None:
    with pytest.raises(LocalConfigError) as exc:
        load_local_config(cicd, environ={})  # ADMIN_ENV not exported
    assert ADMIN_ENV in str(exc.value)


def test_empty_required_token_env_var_is_named(cicd: Path) -> None:
    with pytest.raises(LocalConfigError) as exc:
        load_local_config(cicd, environ={ADMIN_ENV: "   "})  # blank == unset
    assert ADMIN_ENV in str(exc.value)


# ---------------------------------------------------------------------------
# 7. CRITICAL (R9.3 / R6.6) — secret values never appear in logs/repr/str/errors
# ---------------------------------------------------------------------------
def test_secret_value_never_appears_in_repr_or_str(cicd: Path) -> None:
    config = load_local_config(cicd, environ=_env())

    for rendered in (str(config), repr(config), str(config.secrets), repr(config.secrets)):
        assert SECRET_SENTINEL not in rendered
        assert TOKEN_SENTINEL not in rendered

    # The explicit reveal STILL returns the true values (opt-in works).
    assert config.get_secret("ECR_REPO") == SECRET_SENTINEL
    assert config.get_env_token(ADMIN_ENV) == TOKEN_SENTINEL

    # Keys remain visible (the store/config is safe to log).
    assert "ECR_REPO" in repr(config.secrets)
    assert "ECR_REPO" in repr(config)


def test_secret_value_never_leaks_into_any_error(tmp_path: Path, caplog) -> None:
    """Across every failure mode, the raised error text must not contain a value."""
    caplog.set_level("DEBUG")

    # Each entry builds a failing (bootstrap, secrets, environ) that still embeds
    # the real sentinel value somewhere, then asserts the error never echoes it.
    scenarios: list[tuple[dict | None, dict | None, dict]] = []

    # a) stray uncatalogued value carrying the sentinel
    scenarios.append(
        (_valid_bootstrap(), {"ECR_REPO": "ok", "STRAY": SECRET_SENTINEL}, _env())
    )
    # b) missing secret value while the env token carries the sentinel-ish token
    scenarios.append((_valid_bootstrap(), {}, _env()))
    # c) unset token env var (environ holds the sentinel under an unrelated name)
    scenarios.append(
        (_valid_bootstrap(), _valid_secrets(), {"UNRELATED": TOKEN_SENTINEL})
    )
    # d) unknown extra key alongside a real secret value present
    bad = _valid_bootstrap()
    bad["totally_unknown_key"] = SECRET_SENTINEL
    scenarios.append((bad, _valid_secrets(), _env()))

    for i, (bootstrap, secrets, environ) in enumerate(scenarios):
        cicd = _write_cicd(tmp_path / f".cicd_{i}", bootstrap, secrets)
        with pytest.raises(LocalConfigError) as exc:
            load_local_config(cicd, environ=environ)
        message = str(exc.value)
        assert SECRET_SENTINEL not in message, f"scenario {i} leaked a secret value"
        assert TOKEN_SENTINEL not in message, f"scenario {i} leaked a token value"

    # Nothing sensitive was emitted to the log stream either.
    assert SECRET_SENTINEL not in caplog.text
    assert TOKEN_SENTINEL not in caplog.text


def test_secretstore_from_name_value_map_redacts(caplog) -> None:
    caplog.set_level("DEBUG")
    store = SecretStore.from_name_value_map(["ECR_REPO"], {"ECR_REPO": SECRET_SENTINEL})

    assert SECRET_SENTINEL not in repr(store)
    assert SECRET_SENTINEL not in str(store)
    assert "ECR_REPO" in repr(store)
    assert store.get_secret_value("ECR_REPO") == SECRET_SENTINEL
    assert SECRET_SENTINEL not in caplog.text


# ---------------------------------------------------------------------------
# 8. extra="forbid" — unknown key rejected
# ---------------------------------------------------------------------------
def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["mystery_knob"] = True
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    with pytest.raises(LocalConfigError) as exc:
        load_local_config(cicd, environ=_env())
    message = str(exc.value)
    assert "mystery_knob" in message
    assert "Extra inputs are not permitted" in message


def test_unknown_nested_key_is_rejected(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["repo"]["unexpected"] = "x"
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    with pytest.raises(LocalConfigError) as exc:
        load_local_config(cicd, environ=_env())
    assert "unexpected" in str(exc.value)


# ---------------------------------------------------------------------------
# misc — missing file surfaces a clear, value-free error
# ---------------------------------------------------------------------------
def test_missing_bootstrap_file_is_reported(tmp_path: Path) -> None:
    cicd = _write_cicd(tmp_path / ".cicd", None, _valid_secrets())
    with pytest.raises(LocalConfigError) as exc:
        load_local_config(cicd, environ=_env())
    assert "bootstrap.local.yaml" in str(exc.value)
