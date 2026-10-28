"""Unit tests for the "automated vs manual" bootstrap report (task 9.3).

Covers the honest summary design §6.5 / §10 calls for (R7) and its value-free
contract (R9.3 / R6.5):

    * an AUTOMATED section lists the plan's automatable actions (secret pushes,
      branch protection, OIDC, repo, runners);
    * a MANUAL section lists the irreducible prerequisites derived from the config:
        - ALWAYS: export the vendor admin token env var (by NAME);
        - the account/org-level AWS IAM OIDC provider step when ``oidc.aws`` has no
          ``provider_arn`` — and NOT when a ``provider_arn`` is already recorded;
        - the Azure tenant-level app-registration step when ``oidc.azure`` is set;
        - the self-hosted runner registration-token step (by env-var NAME);
        - a Databricks account-level workspace/metastore note when a databricks
          target is configured;
    * the one-line summary reports "Automated N of M steps; K manual (see runbook)";
    * the whole report is VALUE-FREE — no secret/token value appears (R9.3);
    * a missing REQUIRED key fails fast naming the key (R6.5).

Fixtures write real ``.cicd`` files under ``tmp_path`` and inject an ``environ`` dict,
so tests never touch the developer's real config or process environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cicdgen.bootstrap import (
    AutomationReport,
    build_automation_report,
    emit_automation_report,
)
from cicdgen.localconfig import LocalConfigError

# Real-looking values that MUST NEVER leak into the report (R9.3).
SECRET_SENTINEL = "s3cr3t-value-SHOULD-NEVER-LEAK-42"
TOKEN_SENTINEL = "ghp_adminTOKEN_SHOULD_NEVER_LEAK_99"
RUNNER_TOKEN_SENTINEL = "runnerREGTOKEN_SHOULD_NEVER_LEAK_77"
ADMIN_ENV = "CICD_ADMIN_TOKEN"
RUNNER_ENV = "RUNNER_TOKEN"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def _valid_bootstrap() -> dict:
    """A complete, valid bootstrap mapping: aws OIDC (no provider_arn), shared
    runners, 2 branches, 2 secrets."""
    return {
        "repo": {
            "owner": "my-org",
            "name": "my-service",
            "create_if_missing": True,
            "default_branch": "main",
        },
        "vendor_admin": {"token_env": ADMIN_ENV},
        "runners": {"mode": "shared"},
        "oidc": {
            "aws": {
                "account_id": "123456789012",
                "role_name": "my-deploy-role",
                "region": "eu-west-1",
            }
        },
        "branch_protection": {
            "main": {"required_approvals": 2, "required_checks": ["build", "test"]},
            "release": {"required_approvals": 1, "required_checks": []},
        },
        "target": {
            "aws-ecs": {
                "ecr_repo_secret": "ECR_REPO",
                "cluster": "my-cluster",
                "service": "my-service",
                "task_family": "my-service-task",
            }
        },
        "secrets": ["ECR_REPO", "EXTRA_SECRET"],
    }


def _valid_secrets() -> dict:
    return {"ECR_REPO": SECRET_SENTINEL, "EXTRA_SECRET": "another-" + SECRET_SENTINEL}


def _write_cicd(cicd_dir: Path, bootstrap: dict | None, secrets: dict | None) -> Path:
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


def _env(**extra: str) -> dict[str, str]:
    base = {ADMIN_ENV: TOKEN_SENTINEL}
    base.update(extra)
    return base


@pytest.fixture
def cicd(tmp_path: Path) -> Path:
    return _write_cicd(tmp_path / ".cicd", _valid_bootstrap(), _valid_secrets())


# ---------------------------------------------------------------------------
# 1. AUTOMATED section lists the automatable actions
# ---------------------------------------------------------------------------
def test_automated_section_lists_automatable_actions(cicd: Path) -> None:
    report = emit_automation_report(cicd, environ=_env())

    assert "AUTOMATED" in report
    # secret pushes (by NAME), branch protection, repo, OIDC are all automated bits
    assert "ECR_REPO" in report
    assert "EXTRA_SECRET" in report
    assert "branch protection" in report.lower()
    assert "my-org/my-service" in report


def test_structured_automated_matches_automatable_plan_actions(cicd: Path) -> None:
    model = build_automation_report(cicd, environ=_env())
    assert isinstance(model, AutomationReport)
    assert model.automated, "expected automatable actions"
    assert all(a.automatable for a in model.automated)
    # the plan for this config has no manual actions, so automated == every action
    assert model.automated_count >= 1


# ---------------------------------------------------------------------------
# 2. MANUAL section — admin token (ALWAYS) + org/account-level AWS OIDC provider
# ---------------------------------------------------------------------------
def test_manual_section_lists_admin_token_export_by_name(cicd: Path) -> None:
    report = emit_automation_report(cicd, environ=_env())

    assert "MANUAL" in report
    # admin token export referenced by env-var NAME, never its value
    assert ADMIN_ENV in report
    assert TOKEN_SENTINEL not in report


def test_manual_section_lists_aws_oidc_provider_when_no_provider_arn(cicd: Path) -> None:
    model = build_automation_report(cicd, environ=_env())
    concerns = {s.concern for s in model.manual}
    assert "oidc" in concerns
    oidc_text = " ".join(s.description for s in model.manual if s.concern == "oidc")
    assert "OIDC identity provider" in oidc_text
    assert "provider_arn" in oidc_text


def test_no_aws_oidc_manual_step_when_provider_arn_present(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["oidc"]["aws"]["provider_arn"] = (
        "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    )
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    model = build_automation_report(cicd, environ=_env())
    oidc_steps = [s for s in model.manual if s.concern == "oidc"]
    assert oidc_steps == [], "a recorded provider_arn means the IdP step is not manual"


def test_azure_oidc_adds_tenant_level_manual_step(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["oidc"] = {
        "azure": {
            "tenant_id": "tenant-1",
            "subscription_id": "sub-1",
            "client_id": "client-1",
        }
    }
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    model = build_automation_report(cicd, environ=_env())
    oidc_text = " ".join(s.description for s in model.manual if s.concern == "oidc")
    assert "Azure app registration" in oidc_text
    assert "tenant" in oidc_text.lower()


# ---------------------------------------------------------------------------
# 3. self-hosted runners add the runner-token manual step (by env-var NAME)
# ---------------------------------------------------------------------------
def test_self_hosted_runner_adds_manual_token_step_by_name(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["runners"] = {
        "mode": "self-hosted",
        "registration_token_env": RUNNER_ENV,
        "labels": ["linux", "x64"],
    }
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    report = emit_automation_report(
        cicd, environ=_env(**{RUNNER_ENV: RUNNER_TOKEN_SENTINEL})
    )
    assert RUNNER_ENV in report  # env-var NAME present
    assert RUNNER_TOKEN_SENTINEL not in report  # token VALUE absent (R9.3)

    model = build_automation_report(
        cicd, environ=_env(**{RUNNER_ENV: RUNNER_TOKEN_SENTINEL})
    )
    runner_steps = [s for s in model.manual if s.concern == "runners"]
    assert len(runner_steps) == 1
    assert RUNNER_ENV in runner_steps[0].description


def test_shared_runners_add_no_manual_runner_step(cicd: Path) -> None:
    model = build_automation_report(cicd, environ=_env())
    assert [s for s in model.manual if s.concern == "runners"] == []


# ---------------------------------------------------------------------------
# 4. databricks target adds an account-level manual note (data-driven)
# ---------------------------------------------------------------------------
def test_databricks_target_adds_account_level_manual_note(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["target"] = {
        "databricks": {
            "host_secret": "DBX_HOST",
            "token_secret": "DBX_TOKEN",
            "job_name": "my-job",
        }
    }
    bootstrap["secrets"] = ["DBX_HOST", "DBX_TOKEN"]
    secrets = {"DBX_HOST": "https://example", "DBX_TOKEN": SECRET_SENTINEL}
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, secrets)

    model = build_automation_report(cicd, environ=_env())
    target_steps = [s for s in model.manual if s.concern == "target"]
    assert len(target_steps) == 1
    assert "metastore" in target_steps[0].description.lower()


# ---------------------------------------------------------------------------
# 5. the one-line summary counts automated N of M, manual K
# ---------------------------------------------------------------------------
def test_summary_counts_automated_and_manual(cicd: Path) -> None:
    model = build_automation_report(cicd, environ=_env())
    # this config: admin-token (always) + aws-oidc-provider (no arn) = 2 manual
    assert model.manual_count == 2
    expected = (
        f"Automated {model.automated_count} of {model.total_count} steps; "
        f"{model.manual_count} manual (see MIGRATION_RUNBOOK.md)."
    )
    assert model.summary == expected
    assert expected in emit_automation_report(cicd, environ=_env())
    assert model.total_count == model.automated_count + model.manual_count


# ---------------------------------------------------------------------------
# 6. value-free end to end (R9.3)
# ---------------------------------------------------------------------------
def test_report_is_value_free(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["runners"] = {
        "mode": "self-hosted",
        "registration_token_env": RUNNER_ENV,
    }
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    report = emit_automation_report(
        cicd, environ=_env(**{RUNNER_ENV: RUNNER_TOKEN_SENTINEL})
    )
    assert SECRET_SENTINEL not in report
    assert TOKEN_SENTINEL not in report
    assert RUNNER_TOKEN_SENTINEL not in report
    # but NAMES remain (useful + safe to log)
    assert ADMIN_ENV in report
    assert RUNNER_ENV in report
    assert "ECR_REPO" in report


def test_report_points_at_runbook(cicd: Path) -> None:
    report = emit_automation_report(cicd, environ=_env())
    assert "MIGRATION_RUNBOOK.md" in report


# ---------------------------------------------------------------------------
# 7. fail-fast on a missing required key / unset token env var (R6.5)
# ---------------------------------------------------------------------------
def test_missing_required_key_fails_fast_naming_the_key(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    del bootstrap["vendor_admin"]
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    with pytest.raises(LocalConfigError) as exc:
        emit_automation_report(cicd, environ=_env())
    assert "vendor_admin" in str(exc.value)


def test_unset_admin_token_env_fails_fast_naming_the_env_var(cicd: Path) -> None:
    with pytest.raises(LocalConfigError) as exc:
        emit_automation_report(cicd, environ={})  # ADMIN_ENV not exported
    assert ADMIN_ENV in str(exc.value)
