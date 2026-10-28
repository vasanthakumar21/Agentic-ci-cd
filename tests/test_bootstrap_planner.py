"""Unit tests for the dry-run bootstrap planner (task 9.1).

Covers the PLANNING half of the bootstrap flow (design §6, R7) and its hard
value-free contract (R9.3 / R6.5):

    * a plan action exists for EACH catalogued secret, described by NAME — and no
      secret VALUE appears in the plan or its rendered report (R9.3);
    * OIDC actions reflect the configured cloud(s): aws-only, azure-only, both, none;
    * self-hosted vs shared runner handling (and the token is referenced by env-var
      NAME only, never its value);
    * one branch-protection action per configured branch;
    * the repo-creation action is gated by ``create_if_missing``;
    * the planner is dry-run by default and marks nothing as executed;
    * a missing REQUIRED key aborts with ``LocalConfigError`` naming the key (R6.5).

Fixtures write real ``.cicd`` files under ``tmp_path`` and inject an ``environ`` dict,
so tests never touch the developer's real config or process environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cicdgen.bootstrap import (
    BootstrapAction,
    BootstrapPlan,
    Concern,
    plan_bootstrap,
    render_plan,
)
from cicdgen.localconfig import LocalConfigError

# Real-looking values that MUST NEVER leak into the plan or report (R9.3).
SECRET_SENTINEL = "s3cr3t-value-SHOULD-NEVER-LEAK-42"
TOKEN_SENTINEL = "ghp_adminTOKEN_SHOULD_NEVER_LEAK_99"
RUNNER_TOKEN_SENTINEL = "runnerREGTOKEN_SHOULD_NEVER_LEAK_77"
ADMIN_ENV = "CICD_ADMIN_TOKEN"
RUNNER_ENV = "RUNNER_TOKEN"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def _valid_bootstrap() -> dict:
    """A complete, valid bootstrap mapping: aws OIDC, shared runners, 2 branches."""
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
# 1. shape + dry-run defaults
# ---------------------------------------------------------------------------
def test_plan_bootstrap_returns_dry_run_plan(cicd: Path) -> None:
    plan = plan_bootstrap(cicd, environ=_env())

    assert isinstance(plan, BootstrapPlan)
    assert plan.dry_run is True
    assert plan.repo == "my-org/my-service"
    assert plan.actions, "plan should contain actions"
    # The planner NEVER executes: everything stays a placeholder.
    assert all(isinstance(a, BootstrapAction) for a in plan.actions)
    assert all(a.executed is False for a in plan.actions)


def test_plan_actions_are_ordered_by_concern(cicd: Path) -> None:
    plan = plan_bootstrap(cicd, environ=_env())
    order = {
        Concern.REPO: 0,
        Concern.SECRETS: 1,
        Concern.OIDC: 2,
        Concern.RUNNERS: 3,
        Concern.BRANCH_PROTECTION: 4,
    }
    ranks = [order[a.concern] for a in plan.actions]
    assert ranks == sorted(ranks), "actions must be grouped in concern order"


# ---------------------------------------------------------------------------
# 2. secrets — one action per NAME, values absent (R9.3)
# ---------------------------------------------------------------------------
def test_one_action_per_secret_by_name(cicd: Path) -> None:
    plan = plan_bootstrap(cicd, environ=_env())
    secret_actions = plan.by_concern(Concern.SECRETS)

    assert len(secret_actions) == 2
    blob = " ".join(a.description for a in secret_actions)
    assert "ECR_REPO" in blob
    assert "EXTRA_SECRET" in blob


def test_secret_values_absent_from_plan_and_report(cicd: Path) -> None:
    plan = plan_bootstrap(cicd, environ=_env())
    report = render_plan(plan)

    # No secret or token value anywhere in the action descriptions...
    for action in plan.actions:
        assert SECRET_SENTINEL not in action.description
        assert TOKEN_SENTINEL not in action.description
    # ...nor in the rendered report.
    assert SECRET_SENTINEL not in report
    assert TOKEN_SENTINEL not in report
    # But secret NAMES remain visible (the report is useful + safe to log).
    assert "ECR_REPO" in report
    assert "EXTRA_SECRET" in report


# ---------------------------------------------------------------------------
# 3. OIDC — reflects the configured cloud(s)
# ---------------------------------------------------------------------------
def test_oidc_aws_only(cicd: Path) -> None:
    plan = plan_bootstrap(cicd, environ=_env())
    oidc = plan.by_concern(Concern.OIDC)
    assert len(oidc) == 1
    desc = oidc[0].description
    assert "AWS" in desc
    assert "my-deploy-role" in desc
    assert "123456789012" in desc


def test_oidc_azure_only(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["oidc"] = {
        "azure": {
            "tenant_id": "tenant-1",
            "subscription_id": "sub-1",
            "client_id": "client-1",
        }
    }
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    plan = plan_bootstrap(cicd, environ=_env())
    oidc = plan.by_concern(Concern.OIDC)
    assert len(oidc) == 1
    assert "Azure" in oidc[0].description
    assert "client-1" in oidc[0].description


def test_oidc_both_clouds(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["oidc"] = {
        "aws": {
            "account_id": "123456789012",
            "role_name": "my-deploy-role",
            "region": "eu-west-1",
        },
        "azure": {
            "tenant_id": "tenant-1",
            "subscription_id": "sub-1",
            "client_id": "client-1",
        },
    }
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    plan = plan_bootstrap(cicd, environ=_env())
    oidc = plan.by_concern(Concern.OIDC)
    assert len(oidc) == 2
    clouds = " ".join(a.description for a in oidc)
    assert "AWS" in clouds and "Azure" in clouds


def test_oidc_none_configured_is_a_skip_note(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap.pop("oidc")
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    plan = plan_bootstrap(cicd, environ=_env())
    oidc = plan.by_concern(Concern.OIDC)
    assert len(oidc) == 1
    assert "No OIDC" in oidc[0].description


# ---------------------------------------------------------------------------
# 4. runners — shared vs self-hosted
# ---------------------------------------------------------------------------
def test_shared_runners_is_a_noop_note(cicd: Path) -> None:
    plan = plan_bootstrap(cicd, environ=_env())
    runners = plan.by_concern(Concern.RUNNERS)
    assert len(runners) == 1
    assert "shared" in runners[0].description.lower()


def test_self_hosted_runner_registration_references_env_name_only(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["runners"] = {
        "mode": "self-hosted",
        "registration_token_env": RUNNER_ENV,
        "labels": ["linux", "x64"],
    }
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    plan = plan_bootstrap(
        cicd, environ=_env(**{RUNNER_ENV: RUNNER_TOKEN_SENTINEL})
    )
    runners = plan.by_concern(Concern.RUNNERS)
    assert len(runners) == 1
    desc = runners[0].description
    assert RUNNER_ENV in desc  # env-var NAME present
    assert RUNNER_TOKEN_SENTINEL not in desc  # token VALUE absent (R9.3)
    assert RUNNER_TOKEN_SENTINEL not in render_plan(plan)


# ---------------------------------------------------------------------------
# 5. branch protection — one action per branch
# ---------------------------------------------------------------------------
def test_one_branch_protection_action_per_branch(cicd: Path) -> None:
    plan = plan_bootstrap(cicd, environ=_env())
    bp = plan.by_concern(Concern.BRANCH_PROTECTION)
    assert len(bp) == 2
    branches = " ".join(a.description for a in bp)
    assert "main" in branches and "release" in branches
    # required_approvals surfaced
    assert "2 approval" in branches


def test_no_branch_protection_configured_yields_no_actions(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap.pop("branch_protection")
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    plan = plan_bootstrap(cicd, environ=_env())
    assert plan.by_concern(Concern.BRANCH_PROTECTION) == []


# ---------------------------------------------------------------------------
# 6. repo creation — gated by create_if_missing
# ---------------------------------------------------------------------------
def test_repo_create_action_when_create_if_missing_true(cicd: Path) -> None:
    plan = plan_bootstrap(cicd, environ=_env())
    repo = plan.by_concern(Concern.REPO)
    assert len(repo) == 1
    assert "Create repository" in repo[0].description
    assert "my-org/my-service" in repo[0].description


def test_repo_assumed_existing_when_create_if_missing_false(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["repo"]["create_if_missing"] = False
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    plan = plan_bootstrap(cicd, environ=_env())
    repo = plan.by_concern(Concern.REPO)
    assert len(repo) == 1
    assert "already exist" in repo[0].description
    assert "Create repository" not in repo[0].description


# ---------------------------------------------------------------------------
# 7. fail-fast on a missing required key (R6.5)
# ---------------------------------------------------------------------------
def test_missing_required_key_aborts_naming_the_key(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    del bootstrap["vendor_admin"]
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())

    with pytest.raises(LocalConfigError) as exc:
        plan_bootstrap(cicd, environ=_env())
    assert "vendor_admin" in str(exc.value)


def test_missing_token_env_aborts_naming_the_env_var(cicd: Path) -> None:
    with pytest.raises(LocalConfigError) as exc:
        plan_bootstrap(cicd, environ={})  # ADMIN_ENV not exported
    assert ADMIN_ENV in str(exc.value)


# ---------------------------------------------------------------------------
# 8. rendered report — summary + value-free end to end
# ---------------------------------------------------------------------------
def test_rendered_report_has_dry_run_banner_and_counts(cicd: Path) -> None:
    plan = plan_bootstrap(cicd, environ=_env())
    report = render_plan(plan)

    assert "DRY RUN" in report
    assert "my-org/my-service" in report
    assert f"{len(plan.actions)} planned action" in report
    # every secret name is still present (useful report)
    assert "ECR_REPO" in report
