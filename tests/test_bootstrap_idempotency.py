"""End-to-end idempotency + fail-fast guarantees for the bootstrapper (task 9.4).

This is the DEDICATED consolidation test for the two headline bootstrap contracts,
exercised deeper and more end-to-end than ``test_bootstrap_apply.py`` (which covers
the first/second-apply mechanics per concern). Here we prove, through the public
:func:`apply_bootstrap` entry point and a recording :class:`FakeExecutor`:

    * **Idempotent re-run (R7).** Applying three times in a row reaches a stable
      fixed point: pass 1 creates everything; passes 2 and 3 are fully UNCHANGED
      with ZERO additional mutating calls, and the executor's resource store is
      byte-identical after pass 2 and pass 3. ``ApplyResult.changed`` is True only
      on pass 1.
    * **Convergence from partial state (R7).** Seeding SOME resources as
      pre-existing yields a mixed CREATED/UNCHANGED first apply that creates only
      the gaps, and a second apply that is fully UNCHANGED — no duplicates.
    * **Drift re-apply (R7).** Mutating a resource out from under the bootstrapper
      (simulated drift) is detected and corrected (UPDATED) on the next apply, then
      UNCHANGED thereafter.
    * **Missing local key aborts with a clear message (R6.5).** Dropping any required
      key / field (or unsetting the admin token env var) raises
      :class:`LocalConfigError` whose message NAMES the offending key, and NO
      mutating executor call happens before the abort (fail-fast before side effects).
    * **Dry-run by default (R9.2).** Without ``confirm`` the apply mutates nothing,
      even against an empty environment; mutation requires an explicit ``confirm=True``.

The :class:`FakeExecutor` here is a self-contained mirror of the one in
``test_bootstrap_apply.py`` plus a ``snapshot()`` helper that captures the full
resource store, so fixed-point stability can be asserted directly.
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
)
from cicdgen.bootstrap.planner import Concern
from cicdgen.localconfig import LocalConfigError

SECRET_ECR = "s3cr3t-ecr-SHOULD-NEVER-LEAK-42"
SECRET_EXTRA = "another-s3cr3t-SHOULD-NEVER-LEAK-88"
ADMIN_TOKEN = "ghp_adminTOKEN_SHOULD_NEVER_LEAK_99"
ADMIN_ENV = "CICD_ADMIN_TOKEN"


# ---------------------------------------------------------------------------
# FakeExecutor — in-memory resource store that records every call, with a
# snapshot() helper so a test can assert a stable fixed point across re-runs.
# ---------------------------------------------------------------------------
class FakeExecutor:
    """Simulates vendor/cloud state + records calls, for idempotency assertions."""

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

    def mutating_calls(self) -> dict[str, int]:
        return {
            "create_repo": self.calls["create_repo"],
            "push_secret": self.calls["push_secret"],
            "create_oidc_trust": self.calls["create_oidc_trust"],
            "register_runner": self.calls["register_runner"],
            "set_branch_protection": self.calls["set_branch_protection"],
        }

    def snapshot(self) -> dict[str, object]:
        """A hashable-enough, comparable snapshot of the full resource store."""
        return {
            "repo_present": self._repo_present,
            "secrets": frozenset(self._secrets),
            "oidc_trusts": frozenset(self._oidc_trusts),
            "runners": frozenset(self._runners),
            "branch_rules": tuple(sorted(self._branch_rules.items())),
        }


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def _valid_bootstrap() -> dict:
    """Complete, valid bootstrap: repo, admin, shared runners, aws OIDC, 2 branches."""
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
    return {"ECR_REPO": SECRET_ECR, "EXTRA_SECRET": SECRET_EXTRA}


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
    base = {ADMIN_ENV: ADMIN_TOKEN}
    base.update(extra)
    return base


@pytest.fixture
def cicd(tmp_path: Path) -> Path:
    return _write_cicd(tmp_path / ".cicd", _valid_bootstrap(), _valid_secrets())


_DESIRED_MAIN = BranchProtectionRule(
    required_approvals=2,
    required_checks=("build", "test"),
    enforce_admins=False,
    dismiss_stale_reviews=True,
)
_DESIRED_RELEASE = BranchProtectionRule(
    required_approvals=1,
    required_checks=(),
    enforce_admins=False,
    dismiss_stale_reviews=True,
)


# ===========================================================================
# 1. Idempotent re-run (R7): THREE passes reach a stable fixed point.
# ===========================================================================
def test_three_applies_reach_a_stable_fixed_point(cicd: Path) -> None:
    fake = FakeExecutor()  # empty environment

    r1 = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    after_pass1 = fake.mutating_calls()
    snap1 = fake.snapshot()

    r2 = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    after_pass2 = fake.mutating_calls()
    snap2 = fake.snapshot()

    r3 = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    after_pass3 = fake.mutating_calls()
    snap3 = fake.snapshot()

    # pass 1 created everything and reported a change.
    assert isinstance(r1, ApplyResult)
    assert r1.changed is True
    assert after_pass1 == {
        "create_repo": 1,
        "push_secret": 2,
        "create_oidc_trust": 1,
        "register_runner": 0,  # shared runners -> nothing to register
        "set_branch_protection": 2,
    }

    # passes 2 and 3 changed NOTHING.
    assert r2.changed is False
    assert r3.changed is False

    # every actionable concern on pass 2 and pass 3 is UNCHANGED.
    actionable = {Concern.REPO, Concern.SECRETS, Concern.OIDC, Concern.BRANCH_PROTECTION}
    for result in (r2, r3):
        acted = [r for r in result.results if r.concern in actionable]
        assert acted
        assert all(r.outcome is Outcome.UNCHANGED for r in acted)

    # NO additional mutating calls accumulated after the first pass.
    assert after_pass2 == after_pass1
    assert after_pass3 == after_pass1

    # the resource store is a STABLE fixed point from pass 1 onward.
    assert snap1 == snap2 == snap3


def test_no_duplicate_resources_after_repeated_applies(cicd: Path) -> None:
    fake = FakeExecutor()
    for _ in range(3):
        apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)

    # exactly one repo, two secrets, one oidc trust, two branch rules — no dupes.
    assert fake._repo_present is True
    assert fake._secrets == {"ECR_REPO", "EXTRA_SECRET"}
    assert fake._oidc_trusts == {("github-oidc", "my-deploy-role")}
    assert set(fake._branch_rules) == {"main", "release"}
    # each resource was created exactly once across all three passes.
    assert fake.calls["create_repo"] == 1
    assert fake.calls["push_secret"] == 2
    assert fake.calls["create_oidc_trust"] == 1
    assert fake.calls["set_branch_protection"] == 2


# ===========================================================================
# 2. Convergence from partial pre-existing state (R7).
# ===========================================================================
def test_partial_state_creates_only_the_gaps_then_converges(cicd: Path) -> None:
    # Seed: repo present, ONE of two secrets present, ONE of two branch rules
    # present-and-matching. OIDC trust absent. => a mixed CREATED/UNCHANGED apply.
    fake = FakeExecutor(
        repo_present=True,
        secrets={"ECR_REPO"},
        oidc_trusts=set(),
        branch_rules={"release": _DESIRED_RELEASE},
    )

    r1 = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)

    by_concern_target = {(r.concern, r.target): r.outcome for r in r1.results}

    # repo already existed -> UNCHANGED, no create.
    assert by_concern_target[(Concern.REPO, "my-org/my-service")] is Outcome.UNCHANGED
    assert fake.calls["create_repo"] == 0

    # ECR_REPO existed (UNCHANGED); EXTRA_SECRET was the gap (CREATED).
    assert by_concern_target[(Concern.SECRETS, "ECR_REPO")] is Outcome.UNCHANGED
    assert by_concern_target[(Concern.SECRETS, "EXTRA_SECRET")] is Outcome.CREATED
    assert fake.calls["push_secret"] == 1  # only the missing one

    # OIDC trust was absent -> CREATED.
    assert by_concern_target[(Concern.OIDC, "my-deploy-role")] is Outcome.CREATED
    assert fake.calls["create_oidc_trust"] == 1

    # 'release' matched (UNCHANGED); 'main' was the gap (CREATED).
    assert by_concern_target[(Concern.BRANCH_PROTECTION, "release")] is Outcome.UNCHANGED
    assert by_concern_target[(Concern.BRANCH_PROTECTION, "main")] is Outcome.CREATED
    assert fake.calls["set_branch_protection"] == 1  # only the missing branch

    assert r1.changed is True  # we did fill gaps

    # a re-run from the now-complete state is fully UNCHANGED, no new mutations.
    mutations_after_first = fake.mutating_calls()
    r2 = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    assert r2.changed is False
    assert fake.mutating_calls() == mutations_after_first
    actionable = {Concern.REPO, Concern.SECRETS, Concern.OIDC, Concern.BRANCH_PROTECTION}
    assert all(
        r.outcome is Outcome.UNCHANGED for r in r2.results if r.concern in actionable
    )


# ===========================================================================
# 3. Drift re-apply (R7): external mutation is corrected, then stays UNCHANGED.
# ===========================================================================
def test_drift_is_reconciled_on_reapply_then_stable(cicd: Path) -> None:
    fake = FakeExecutor()

    # Converge first.
    apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    assert fake._branch_rules["main"] == _DESIRED_MAIN

    # Simulate drift: someone weakens 'main' protection out from under us.
    drifted = BranchProtectionRule(
        required_approvals=0,  # config wants 2
        required_checks=(),  # config wants build,test
        enforce_admins=False,
        dismiss_stale_reviews=True,
    )
    fake._branch_rules["main"] = drifted
    set_calls_before = fake.calls["set_branch_protection"]

    # Re-apply detects the divergence and corrects it.
    r_drift = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    main_result = next(
        r
        for r in r_drift.results
        if r.concern is Concern.BRANCH_PROTECTION and r.target == "main"
    )
    assert main_result.outcome is Outcome.UPDATED
    assert r_drift.changed is True
    # only 'main' was rewritten (release still matched).
    assert fake.calls["set_branch_protection"] == set_calls_before + 1
    assert fake._branch_rules["main"] == _DESIRED_MAIN

    # And the run AFTER reconciliation is UNCHANGED again (back to fixed point).
    r_stable = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    assert r_stable.changed is False
    assert fake.calls["set_branch_protection"] == set_calls_before + 1  # no more writes


# ===========================================================================
# 4. Missing local key aborts with a clear message (R6.5), fail-fast.
# ===========================================================================
def _bootstrap_without(*keys: str) -> dict:
    data = _valid_bootstrap()
    for key in keys:
        data.pop(key, None)
    return data


# Each case: (id, mutate bootstrap/secrets/env) -> expected substring naming the key.
def test_missing_repo_key_aborts_naming_it(tmp_path: Path) -> None:
    cicd = _write_cicd(tmp_path / ".cicd", _bootstrap_without("repo"), _valid_secrets())
    fake = FakeExecutor()
    with pytest.raises(LocalConfigError) as exc:
        apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    assert "repo" in str(exc.value)
    assert fake.mutating_call_count == 0


def test_missing_vendor_admin_key_aborts_naming_it(tmp_path: Path) -> None:
    cicd = _write_cicd(
        tmp_path / ".cicd", _bootstrap_without("vendor_admin"), _valid_secrets()
    )
    fake = FakeExecutor()
    with pytest.raises(LocalConfigError) as exc:
        apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    assert "vendor_admin" in str(exc.value)
    assert fake.mutating_call_count == 0


def test_missing_runners_key_aborts_naming_it(tmp_path: Path) -> None:
    cicd = _write_cicd(
        tmp_path / ".cicd", _bootstrap_without("runners"), _valid_secrets()
    )
    fake = FakeExecutor()
    with pytest.raises(LocalConfigError) as exc:
        apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    assert "runners" in str(exc.value)
    assert fake.mutating_call_count == 0


def test_empty_secret_value_aborts_naming_the_secret(tmp_path: Path) -> None:
    secrets = _valid_secrets()
    secrets["EXTRA_SECRET"] = ""  # empty string is treated as MISSING (R6.5)
    cicd = _write_cicd(tmp_path / ".cicd", _valid_bootstrap(), secrets)
    fake = FakeExecutor()
    with pytest.raises(LocalConfigError) as exc:
        apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    assert "EXTRA_SECRET" in str(exc.value)
    assert fake.mutating_call_count == 0


def test_unset_admin_token_env_aborts_naming_the_var(cicd: Path) -> None:
    fake = FakeExecutor()
    with pytest.raises(LocalConfigError) as exc:
        apply_bootstrap(cicd, executor=fake, environ={}, confirm=True)  # token unset
    assert ADMIN_ENV in str(exc.value)
    assert fake.mutating_call_count == 0


def test_self_hosted_without_registration_token_env_aborts_naming_it(
    tmp_path: Path,
) -> None:
    bootstrap = _valid_bootstrap()
    bootstrap["runners"] = {"mode": "self-hosted"}  # missing registration_token_env
    cicd = _write_cicd(tmp_path / ".cicd", bootstrap, _valid_secrets())
    fake = FakeExecutor()
    with pytest.raises(LocalConfigError) as exc:
        apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    assert "registration_token_env" in str(exc.value)
    assert fake.mutating_call_count == 0


# ===========================================================================
# 5. Dry-run default (R9.2): no confirm => no mutations; confirm required to act.
# ===========================================================================
def test_dry_run_default_mutates_nothing_even_with_empty_env(cicd: Path) -> None:
    fake = FakeExecutor()
    result = apply_bootstrap(cicd, executor=fake, environ=_env())  # confirm defaults False

    assert result.dry_run is True
    assert result.results  # it still describes planned actions
    assert all(r.outcome is Outcome.SKIPPED for r in result.results)
    assert fake.mutating_call_count == 0


def test_confirm_true_is_required_to_mutate(cicd: Path) -> None:
    fake = FakeExecutor()

    dry = apply_bootstrap(cicd, executor=fake, environ=_env())
    assert dry.dry_run is True
    assert fake.mutating_call_count == 0

    wet = apply_bootstrap(cicd, executor=fake, environ=_env(), confirm=True)
    assert wet.dry_run is False
    assert fake.mutating_call_count > 0
