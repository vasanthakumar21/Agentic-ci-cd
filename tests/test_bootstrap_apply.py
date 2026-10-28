"""Unit tests for the idempotent ``--apply`` bootstrap execution (task 9.2).

Covers the EXECUTION half of the bootstrap flow (design §6, R7) and its three hard
contracts:

    * **Idempotency (R7).** A first apply on empty state CREATES every resource; a
      second apply against the now-populated state yields all UNCHANGED and makes NO
      duplicate create/push calls. A branch-protection rule that differs from current
      is UPDATED.
    * **Dry-run by default (R9.2).** ``confirm=False`` performs ZERO mutating executor
      calls.
    * **Value-free reporting (R9.3 / R6.6).** The executor receives the REAL secret
      value in ``push_secret``, but that value appears NOWHERE in the ``ApplyResult``
      or the rendered report.

And fail-fast on a missing required key (R6.5).

The :class:`FakeExecutor` simulates a resource store and records every call, so the
detect-before-mutate logic is verifiable without any real vendor/cloud account (R9).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

from cicdgen.bootstrap import (
    ApplyResult,
    BranchProtectionRule,
    Outcome,
    apply_bootstrap,
    render_apply_result,
)
from cicdgen.bootstrap.planner import Concern
from cicdgen.localconfig import LocalConfigError

# Real-looking values that MUST NEVER leak into an ApplyResult or its report (R9.3).
SECRET_SENTINEL = "s3cr3t-value-SHOULD-NEVER-LEAK-42"
EXTRA_SECRET_SENTINEL = "another-s3cr3t-SHOULD-NEVER-LEAK-88"
TOKEN_SENTINEL = "ghp_adminTOKEN_SHOULD_NEVER_LEAK_99"
RUNNER_TOKEN_SENTINEL = "runnerREGTOKEN_SHOULD_NEVER_LEAK_77"
ADMIN_ENV = "CICD_ADMIN_TOKEN"
RUNNER_ENV = "RUNNER_TOKEN"


# ---------------------------------------------------------------------------
# FakeExecutor — an in-memory resource store that records every call.
# ---------------------------------------------------------------------------
class FakeExecutor:
    """Simulates vendor/cloud state and records calls for idempotency assertions.

    Pre-seed existing resources via the constructor to simulate a non-empty account;
    an empty FakeExecutor simulates a brand-new environment. Every mutating method
    both updates the in-memory store (so a subsequent detect() sees it) AND bumps a
    call counter, so tests can assert "no duplicate creates on re-run".
    """

    def __init__(
        self,
        *,
        repo_present: bool = False,
        secrets: set[str] | None = None,
        oidc_trusts: set[tuple[str, str]] | None = None,
        runners: set[tuple[str, ...]] | None = None,
        branch_rules: dict[str, BranchProtectionRule] | None = None,
    ) -> None:
        self._repo_present = repo_present
        self._secrets: set[str] = set(secrets or set())
        self._oidc_trusts: set[tuple[str, str]] = set(oidc_trusts or set())
        self._runners: set[tuple[str, ...]] = set(runners or set())
        self._branch_rules: dict[str, BranchProtectionRule] = dict(branch_rules or {})

        # Records the raw values handed to push_secret / register_runner so a test
        # can prove the real value reached the executor (but nowhere else).
        self.pushed_secret_values: dict[str, str] = {}
        self.registered_tokens: list[str] = []

        self.calls: Counter[str] = Counter()

    # --- repo --------------------------------------------------------------
    def repo_exists(self) -> bool:
        self.calls["repo_exists"] += 1
        return self._repo_present

    def create_repo(self) -> None:
        self.calls["create_repo"] += 1
        self._repo_present = True

    # --- secrets -----------------------------------------------------------
    def secret_exists(self, name: str) -> bool:
        self.calls["secret_exists"] += 1
        return name in self._secrets

    def push_secret(self, name: str, value: str) -> None:
        self.calls["push_secret"] += 1
        self._secrets.add(name)
        self.pushed_secret_values[name] = value

    # --- oidc --------------------------------------------------------------
    def oidc_trust_exists(self, provider: str, role_name: str) -> bool:
        self.calls["oidc_trust_exists"] += 1
        return (provider, role_name) in self._oidc_trusts

    def create_oidc_trust(self, provider: str, role_name: str) -> None:
        self.calls["create_oidc_trust"] += 1
        self._oidc_trusts.add((provider, role_name))

    # --- runners -----------------------------------------------------------
    def runner_registered(self, labels: tuple[str, ...]) -> bool:
        self.calls["runner_registered"] += 1
        return labels in self._runners

    def register_runner(self, labels: tuple[str, ...], token: str) -> None:
        self.calls["register_runner"] += 1
        self._runners.add(labels)
        self.registered_tokens.append(token)

    # --- branch protection -------------------------------------------------
    def branch_protection_current(self, branch: str) -> BranchProtectionRule | None:
        self.calls["branch_protection_current"] += 1
        return self._branch_rules.get(branch)

    def set_branch_protection(self, branch: str, rule: BranchProtectionRule) -> None:
        self.calls["set_branch_protection"] += 1
        self._branch_rules[branch] = rule

    # --- helpers for assertions -------------------------------------------
    @property
    def mutating_call_count(self) -> int:
        mutators = (
            "create_repo",
            "push_secret",
            "create_oidc_trust",
            "register_runner",
            "set_branch_protection",
        )
        return sum(self.calls[m] for m in mutators)


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
    return {"ECR_REPO": SECRET_SENTINEL, "EXTRA_SECRET": EXTRA_SECRET_SENTINEL}


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
# 1. first apply on empty env — everything CREATED
# ---------------------------------------------------------------------------
def test_first_apply_creates_every_resource(cicd: Path) -> None:
    fake = FakeExecutor()  # empty environment
    result = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)

    assert isinstance(result, ApplyResult)
    assert result.dry_run is False

    # repo created (create_if_missing is True and it was absent)
    repo_results = [r for r in result.results if r.concern is Concern.REPO]
    assert len(repo_results) == 1
    assert repo_results[0].outcome is Outcome.CREATED
    assert fake.calls["create_repo"] == 1

    # both secrets pushed
    secret_results = [r for r in result.results if r.concern is Concern.SECRETS]
    assert {r.target for r in secret_results} == {"ECR_REPO", "EXTRA_SECRET"}
    assert all(r.outcome is Outcome.CREATED for r in secret_results)
    assert fake.calls["push_secret"] == 2

    # aws oidc trust created
    oidc_results = [r for r in result.results if r.concern is Concern.OIDC]
    assert len(oidc_results) == 1
    assert oidc_results[0].outcome is Outcome.CREATED
    assert fake.calls["create_oidc_trust"] == 1

    # branch protection set on both branches
    bp_results = [r for r in result.results if r.concern is Concern.BRANCH_PROTECTION]
    assert {r.target for r in bp_results} == {"main", "release"}
    assert all(r.outcome is Outcome.CREATED for r in bp_results)
    assert fake.calls["set_branch_protection"] == 2

    assert result.changed is True


# ---------------------------------------------------------------------------
# 2. second apply — all UNCHANGED, no duplicate creates (idempotency, R7)
# ---------------------------------------------------------------------------
def test_second_apply_is_idempotent_no_duplicates(cicd: Path) -> None:
    fake = FakeExecutor()
    apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)

    # snapshot mutating call counts after the first apply
    first_pass = {
        "create_repo": fake.calls["create_repo"],
        "push_secret": fake.calls["push_secret"],
        "create_oidc_trust": fake.calls["create_oidc_trust"],
        "set_branch_protection": fake.calls["set_branch_protection"],
    }

    # run again against the SAME (now-populated) fake
    result2 = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)

    # every actionable concern reports UNCHANGED (shared runners / no-op are SKIPPED)
    actionable = [
        r
        for r in result2.results
        if r.concern in {Concern.REPO, Concern.SECRETS, Concern.OIDC, Concern.BRANCH_PROTECTION}
    ]
    assert actionable, "expected actionable results on re-run"
    assert all(r.outcome is Outcome.UNCHANGED for r in actionable)
    assert result2.changed is False

    # and NO additional mutating calls happened on the second pass
    assert fake.calls["create_repo"] == first_pass["create_repo"]
    assert fake.calls["push_secret"] == first_pass["push_secret"]
    assert fake.calls["create_oidc_trust"] == first_pass["create_oidc_trust"]
    assert fake.calls["set_branch_protection"] == first_pass["set_branch_protection"]


# ---------------------------------------------------------------------------
# 3. branch protection that differs from current -> UPDATED
# ---------------------------------------------------------------------------
def test_branch_protection_differs_is_updated(cicd: Path) -> None:
    # Pre-seed 'main' with a rule that DIFFERS from config (1 approval vs 2) and
    # 'release' with a rule that MATCHES config exactly.
    stale_main = BranchProtectionRule(
        required_approvals=1,  # config wants 2
        required_checks=("build", "test"),
        enforce_admins=False,
        dismiss_stale_reviews=True,
    )
    matching_release = BranchProtectionRule(
        required_approvals=1,
        required_checks=(),
        enforce_admins=False,
        dismiss_stale_reviews=True,
    )
    fake = FakeExecutor(
        repo_present=True,
        secrets={"ECR_REPO", "EXTRA_SECRET"},
        oidc_trusts={("github-oidc", "my-deploy-role")},
        branch_rules={"main": stale_main, "release": matching_release},
    )

    result = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)

    bp = {r.target: r for r in result.results if r.concern is Concern.BRANCH_PROTECTION}
    assert bp["main"].outcome is Outcome.UPDATED
    assert bp["release"].outcome is Outcome.UNCHANGED

    # only the divergent branch was (re)written
    assert fake.calls["set_branch_protection"] == 1
    assert fake._branch_rules["main"].required_approvals == 2


# ---------------------------------------------------------------------------
# 4. dry-run default (confirm=False) — NO mutating calls (R9.2)
# ---------------------------------------------------------------------------
def test_confirm_false_performs_no_mutations(cicd: Path) -> None:
    fake = FakeExecutor()
    result = apply_bootstrap(cicd, executor=fake, environ=_env())  # confirm defaults False

    assert result.dry_run is True
    assert result.results, "dry run should still describe planned actions"
    assert all(r.outcome is Outcome.SKIPPED for r in result.results)

    # No mutating method was ever called on the fake.
    assert fake.calls["create_repo"] == 0
    assert fake.calls["push_secret"] == 0
    assert fake.calls["create_oidc_trust"] == 0
    assert fake.calls["register_runner"] == 0
    assert fake.calls["set_branch_protection"] == 0
    assert fake.mutating_call_count == 0


def test_confirm_false_is_the_default(cicd: Path) -> None:
    fake = FakeExecutor()
    result = apply_bootstrap(cicd, executor=fake, environ=_env())
    assert result.dry_run is True
    assert fake.mutating_call_count == 0


# ---------------------------------------------------------------------------
# 5. value-free (R9.3) — executor gets the real value; results/report do not
# ---------------------------------------------------------------------------
def test_executor_receives_real_secret_but_result_is_value_free(cicd: Path) -> None:
    fake = FakeExecutor()
    result = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)

    # The executor DID receive the real secret values at push time.
    assert fake.pushed_secret_values["ECR_REPO"] == SECRET_SENTINEL
    assert fake.pushed_secret_values["EXTRA_SECRET"] == EXTRA_SECRET_SENTINEL

    # ...but the values appear NOWHERE in the structured result.
    for r in result.results:
        assert SECRET_SENTINEL not in r.detail
        assert EXTRA_SECRET_SENTINEL not in r.detail
        assert TOKEN_SENTINEL not in r.detail
        assert SECRET_SENTINEL not in r.target
        assert EXTRA_SECRET_SENTINEL not in r.target

    # ...nor in the rendered, loggable report.
    report = render_apply_result(result)
    assert SECRET_SENTINEL not in report
    assert EXTRA_SECRET_SENTINEL not in report
    assert TOKEN_SENTINEL not in report
    # But the secret NAMES remain visible (the report stays useful + safe to log).
    assert "ECR_REPO" in report
    assert "EXTRA_SECRET" in report


def test_self_hosted_runner_token_never_leaks(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["runners"] = {
        "mode": "self-hosted",
        "registration_token_env": RUNNER_ENV,
        "labels": ["linux", "x64"],
    }
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())
    fake = FakeExecutor()

    result = apply_bootstrap(
        cicd,
        executor=fake,
        environ=_env(**{RUNNER_ENV: RUNNER_TOKEN_SENTINEL}),
        confirm=True,
    )

    runner_results = [r for r in result.results if r.concern is Concern.RUNNERS]
    assert len(runner_results) == 1
    assert runner_results[0].outcome is Outcome.CREATED
    # executor got the real token...
    assert fake.registered_tokens == [RUNNER_TOKEN_SENTINEL]
    # ...but it leaks nowhere in the result or report.
    report = render_apply_result(result)
    assert RUNNER_TOKEN_SENTINEL not in report
    for r in result.results:
        assert RUNNER_TOKEN_SENTINEL not in r.detail


# ---------------------------------------------------------------------------
# 6. render summary counts
# ---------------------------------------------------------------------------
def test_render_apply_result_has_banner_and_counts(cicd: Path) -> None:
    fake = FakeExecutor()
    result = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    report = render_apply_result(result)

    assert "APPLIED" in report
    assert "my-org/my-service" in report
    assert f"{len(result.results)} action(s)" in report
    assert f"{len(result.created)} created" in report


# ---------------------------------------------------------------------------
# 7. fail-fast on a missing required key (R6.5)
# ---------------------------------------------------------------------------
def test_missing_required_key_aborts_naming_the_key(tmp_path: Path) -> None:
    bootstrap = _valid_bootstrap()
    del bootstrap["vendor_admin"]
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())
    fake = FakeExecutor()

    with pytest.raises(LocalConfigError) as exc:
        apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    assert "vendor_admin" in str(exc.value)
    # nothing was mutated before the abort
    assert fake.mutating_call_count == 0


def test_missing_token_env_aborts_naming_the_env_var(cicd: Path) -> None:
    fake = FakeExecutor()
    with pytest.raises(LocalConfigError) as exc:
        apply_bootstrap(cicd, executor=fake, environ={}, confirm=True)
    assert ADMIN_ENV in str(exc.value)
    assert fake.mutating_call_count == 0
