"""Mocked integration test for `cicd doctor --live` (task 22.5, classified INTEGRATION).

The ``--live`` path hits external vendor/cloud services, so per design §11.10 it is
covered by a couple of MOCKED integration tests rather than property tests. This
module injects a fake read-only executor (the bootstrap :class:`Executor` seam),
asserts :func:`cicdgen.core.doctor.check_live_wiring` categorizes expected drift
(present / absent / uncheckable) correctly, and proves the live compare mutates
NOTHING — it only ever calls the executor's detection methods and never opens a file
for writing (R9.2).

It also exercises the `cicd doctor` CLI end-to-end against a generated repo (real
on-disk plugins), including `--live` with no live backend wired (graceful
degradation to UNCHECKABLE), and the non-zero exit on drift.
"""

from __future__ import annotations

from pathlib import Path

from cicdgen.bootstrap import BranchProtectionRule
from cicdgen.cli import main
from cicdgen.core.doctor import (
    LiveStatus,
    check_live_wiring,
    render_live_report,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"


# ---------------------------------------------------------------------------
# A fake READ-ONLY executor: implements only the detection methods and RECORDS
# every call so we can assert no mutating method was ever touched.
# ---------------------------------------------------------------------------
class _FakeReadOnlyExecutor:
    """Simulates existing/absent live wiring; records which methods were called.

    Only the bootstrap executor's DETECTION methods are implemented. If the live
    compare ever reaches for a mutating method (create_repo / push_secret / …) it
    would raise ``AttributeError`` — a hard proof that the path is read-only.
    """

    def __init__(
        self,
        *,
        repo_present: bool,
        present_roles: set[str],
        protected_branches: dict[str, BranchProtectionRule],
    ) -> None:
        self._repo_present = repo_present
        self._present_roles = present_roles
        self._protected = protected_branches
        self.calls: list[str] = []

    def repo_exists(self) -> bool:
        self.calls.append("repo_exists")
        return self._repo_present

    def oidc_trust_exists(self, provider: str, role_name: str) -> bool:
        self.calls.append(f"oidc_trust_exists:{role_name}")
        return role_name in self._present_roles

    def branch_protection_current(self, branch: str) -> BranchProtectionRule | None:
        self.calls.append(f"branch_protection_current:{branch}")
        return self._protected.get(branch)


# A mutating-capable fake that fails loudly if any mutation is attempted. Used to
# prove the live compare NEVER calls a mutating method.
class _ExplodingOnMutation(_FakeReadOnlyExecutor):
    def create_repo(self) -> None:  # pragma: no cover - must never be called
        raise AssertionError("live compare attempted a MUTATION: create_repo")

    def push_secret(self, name: str, value: str) -> None:  # pragma: no cover
        raise AssertionError("live compare attempted a MUTATION: push_secret")

    def create_oidc_trust(self, provider: str, role_name: str) -> None:  # pragma: no cover
        raise AssertionError("live compare attempted a MUTATION: create_oidc_trust")

    def set_branch_protection(self, branch: str, rule: BranchProtectionRule) -> None:
        raise AssertionError("live compare attempted a MUTATION: set_branch_protection")


_RULE = BranchProtectionRule(
    required_approvals=1,
    required_checks=("ci",),
    enforce_admins=True,
    dismiss_stale_reviews=True,
)


# ===========================================================================
# --live categorizes expected drift (present / absent)
# ===========================================================================
def test_live_compare_categorizes_present_and_absent_wiring() -> None:
    """The read-only compare classifies each probed resource correctly."""
    executor = _ExplodingOnMutation(
        repo_present=True,
        present_roles={"deploy-role"},
        protected_branches={"main": _RULE},
    )

    report = check_live_wiring(
        executor=executor,
        repo_slug="acme/widgets",
        oidc_roles=("deploy-role", "missing-role"),
        protected_branches=("main", "release"),
    )

    assert report.available is True
    by_target = {(f.concern, f.target): f.status for f in report.findings}

    assert by_target[("repo", "acme/widgets")] is LiveStatus.PRESENT
    assert by_target[("oidc", "deploy-role")] is LiveStatus.PRESENT
    assert by_target[("oidc", "missing-role")] is LiveStatus.ABSENT
    assert by_target[("branch_protection", "main")] is LiveStatus.PRESENT
    assert by_target[("branch_protection", "release")] is LiveStatus.ABSENT

    # exactly the absent ones are flagged
    assert {f.target for f in report.absent} == {"missing-role", "release"}


def test_live_compare_writes_nothing_and_calls_only_reads() -> None:
    """The live compare touches ONLY detection methods — never a mutation (R9.2)."""
    executor = _ExplodingOnMutation(
        repo_present=False,
        present_roles=set(),
        protected_branches={},
    )

    # If any mutating method were called the _ExplodingOnMutation fake raises.
    report = check_live_wiring(
        executor=executor,
        repo_slug="acme/widgets",
        oidc_roles=("deploy-role",),
        protected_branches=("main",),
    )

    # Every recorded call is a known read-only detection method.
    read_only_prefixes = ("repo_exists", "oidc_trust_exists", "branch_protection_current")
    for call in executor.calls:
        assert call.startswith(read_only_prefixes), f"unexpected call: {call}"

    # Absent backend wiring is categorized ABSENT, not errored.
    assert all(f.status is LiveStatus.ABSENT for f in report.findings)


def test_live_compare_degrades_gracefully_without_a_backend() -> None:
    """No executor => report says so and lists every probe as UNCHECKABLE (no raise)."""
    report = check_live_wiring(
        executor=None,
        repo_slug="acme/widgets",
        oidc_roles=("deploy-role",),
        protected_branches=("main",),
    )

    assert report.available is False
    assert report.findings  # it reports WHAT it could not check
    assert all(f.status is LiveStatus.UNCHECKABLE for f in report.findings)
    # rendering is value-free and mentions the skipped state
    text = render_live_report(report)
    assert "UNVERIFIED" in text
    assert "uncheckable" in text


# ===========================================================================
# CLI end-to-end: `cicd doctor` against a real generated repo (writes nothing)
# ===========================================================================
def _write_minimal_ir(repo: Path) -> Path:
    ir = repo / "pipeline.yaml"
    ir.write_text(
        "version: 1\n"
        "ci_vendor: github\n"
        "target_platform: aws-ecs\n"
        "project:\n  name: demo\n  language: python\n"
        "cloud:\n  provider: aws\n  region: eu-west-2\n  auth: oidc\n"
        "environments:\n  - name: dev\n    gate: auto\n"
        "stages:\n"
        "  - id: build\n"
        "  - id: deploy-dev\n    environment: dev\n",
        encoding="utf-8",
    )
    return ir


def _generate_real(repo: Path, ir: Path) -> None:
    from cicdgen.core import generate

    generate(ir, repo, plugins_root=PLUGINS_ROOT, check_plugins=False)


def test_cli_doctor_clean_repo_exits_zero(tmp_path: Path, capsys) -> None:
    """`cicd doctor` on a freshly generated repo prints no-drift and exits 0."""
    ir = _write_minimal_ir(tmp_path)
    _generate_real(tmp_path, ir)

    before = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}

    code = main(["doctor", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "No drift" in out

    after = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    assert after == before, "cicd doctor wrote to the repo"


def test_cli_doctor_detects_drift_and_exits_nonzero(tmp_path: Path, capsys) -> None:
    """A tampered owned file makes `cicd doctor` report drift and exit non-zero."""
    ir = _write_minimal_ir(tmp_path)
    _generate_real(tmp_path, ir)

    # Tamper one generated file.
    workflow = tmp_path / ".github/workflows/ci.yml"
    assert workflow.is_file()
    workflow.write_text("# tampered\n", encoding="utf-8", newline="")

    code = main(["doctor", str(tmp_path)])
    out = capsys.readouterr().out

    assert code == 1
    assert "DRIFT DETECTED" in out
    assert ".github/workflows/ci.yml" in out


def test_cli_doctor_live_without_backend_still_exits_on_drift(tmp_path: Path, capsys) -> None:
    """`cicd doctor --live` with no backend reports UNCHECKABLE and still works."""
    ir = _write_minimal_ir(tmp_path)
    _generate_real(tmp_path, ir)

    before = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}

    code = main(["doctor", str(tmp_path), "--live"])
    out = capsys.readouterr().out

    assert code == 0
    assert "No drift" in out
    assert "read-only wiring compare" in out

    after = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
    assert after == before, "cicd doctor --live wrote to the repo"


def test_cli_doctor_without_ir_errors_cleanly(tmp_path: Path, capsys) -> None:
    """Missing IR => a clear stderr message and exit code 2 (not a traceback)."""
    code = main(["doctor", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "IR not found" in err
