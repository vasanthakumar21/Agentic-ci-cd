"""Drift detection — `cicd doctor` core.

A **read-only** engine that compares the committed generated artifacts + manifest
against what the generator WOULD produce now, and reports drift. It writes NOTHING
— it respects the dry-run-by-default posture absolutely: there is no ``--apply``
and no code path here opens a file for writing.

How it works
------------
1. Load the committed ``.cicd/generated.manifest.json`` under ``repo_root`` (the
 paths the generator owns — the "committed" set ``C``) and read the current
 on-disk content of every owned path.
2. Re-run the generation pipeline **in memory** via :func:`cicdgen.core.generate.plan`
 (``generate(..., dry_run=True)``) to get the "would-produce" set ``G``. No files
 are touched — ``plan`` is side-effect-free.
3. Compute drift as a pure diff over paths and content hashes:
 * **missing** — a path in ``G`` not present on disk,
 * **extra** — a path the manifest owns that no longer appears in ``G``,
 * **changed** — a path in both whose committed content ≠ regenerated content,
 * **in-sync** — identical content on disk and in ``G``.
4. Return a structured :class:`DriftReport`. The CLI renders it value-free and
 exits non-zero when any drift exists.

Determinism makes this sound
----------------------------
Because generation is byte-identical for the same IR + repo, "no drift immediately
after ``generate``" is a guarantee, not a hope.

Secrets / value-free
--------------------
A :class:`DriftReport` records PATHS and CATEGORIES only. It never carries file
content, and content hashing goes through :func:`cicdgen.core.manifest.content_hash`
(a pure sha256), so no secret value can leak into the report or any rendering of it.

Vendor-agnostic
---------------
This module branches on nothing vendor- or platform-specific; it reads only the
manifest + the in-memory plan and diffs paths/hashes. It reuses the exact same
``plan`` path every generate uses, so the axes stay entirely in the plugins.

Live compare
------------
:func:`check_live_wiring` additionally queries vendor/cloud wiring in READ-ONLY mode
through the existing bootstrap :class:`~cicdgen.bootstrap.Executor` seam — it only
calls the executor's *detection* methods, never a mutating one, so it writes
nothing. Where live access is not available (no executor / no local config) it
reports what it COULD NOT check rather than failing hard.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from cicdgen.core.generate import GenerationPlan, load_manifest, plan
from cicdgen.core.manifest import MANIFEST_RELPATH, Manifest, content_hash
from cicdgen.ir.models import Pipeline
from cicdgen.plugins.loader import Registry


class DoctorError(RuntimeError):
    """Raised when a drift check cannot run at all (e.g. no committed manifest)."""


class DriftCategory(StrEnum):
    """How one owned path compares between on-disk ``C`` and regenerated ``G``.

 * ``IN_SYNC`` — identical content on disk and in the fresh plan.
 * ``MISSING`` — the generator would produce this path, but it is absent on disk.
 * ``EXTRA`` — the committed manifest owns this path, but a fresh plan no
 longer produces it (stale; a real run would remove it).
 * ``CHANGED`` — present in both, but the committed content differs from what
 the generator would produce now.
 """

    IN_SYNC = "in-sync"
    MISSING = "missing"
    EXTRA = "extra"
    CHANGED = "changed"


@dataclass(frozen=True)
class DriftEntry:
    """One owned path and its drift classification (value-free).

 Records the repo-relative POSIX ``path`` and its :class:`DriftCategory` only —
 never file content. The two hashes (committed vs. regenerated, each produced by
:func:`cicdgen.core.manifest.content_hash`) are retained purely so a report can
 show *that* content differs without ever exposing *what* it is; either may be
 ``None`` when the path is absent on one side.
 """

    path: str
    category: DriftCategory
    committed_hash: str | None = None
    regenerated_hash: str | None = None


@dataclass(frozen=True)
class DriftReport:
    """The structured result of a drift check — pure data, value-free.

 ``entries`` covers every path in the union of the committed manifest and the
 regenerated plan, sorted by path. ``ci_vendor`` / ``target_platform`` are
 echoed from the committed manifest for the report header.
 """

    repo_root: Path
    ci_vendor: str
    target_platform: str
    entries: tuple[DriftEntry, ...]

    # --- category views ----------------------------------------------------
    def _by_category(self, category: DriftCategory) -> tuple[DriftEntry, ...]:
        return tuple(e for e in self.entries if e.category is category)

    @property
    def missing(self) -> tuple[DriftEntry, ...]:
        return self._by_category(DriftCategory.MISSING)

    @property
    def extra(self) -> tuple[DriftEntry, ...]:
        return self._by_category(DriftCategory.EXTRA)

    @property
    def changed(self) -> tuple[DriftEntry, ...]:
        return self._by_category(DriftCategory.CHANGED)

    @property
    def in_sync(self) -> tuple[DriftEntry, ...]:
        return self._by_category(DriftCategory.IN_SYNC)

    @property
    def drifted(self) -> tuple[DriftEntry, ...]:
        """Every entry that is NOT in-sync (missing / extra / changed), sorted."""
        return tuple(e for e in self.entries if e.category is not DriftCategory.IN_SYNC)

    @property
    def has_drift(self) -> bool:
        """True when at least one owned path is missing, extra, or changed."""
        return bool(self.drifted)


# ---------------------------------------------------------------------------
# Pure diff
# ---------------------------------------------------------------------------
def _diff(
    *,
    committed_hashes: Mapping[str, str],
    regenerated_hashes: Mapping[str, str],
) -> tuple[DriftEntry, ...]:
    """Pure path/hash diff of committed (``C``) vs. regenerated (``G``) sets.

 ``committed_hashes`` maps an owned path to the sha256 of its CURRENT on-disk
 content (omit a path that the manifest owns but is absent on disk — that is a
 MISSING). ``regenerated_hashes`` maps a path the fresh plan would produce to the
 sha256 of that produced content. Returns entries sorted by path.
 """
    all_paths = sorted(set(committed_hashes) | set(regenerated_hashes))
    entries: list[DriftEntry] = []
    for path in all_paths:
        disk = committed_hashes.get(path)
        want = regenerated_hashes.get(path)
        if want is not None and disk is None:
            category = DriftCategory.MISSING
        elif want is None and disk is not None:
            category = DriftCategory.EXTRA
        elif disk != want:
            category = DriftCategory.CHANGED
        else:
            category = DriftCategory.IN_SYNC
        entries.append(
            DriftEntry(
                path=path,
                category=category,
                committed_hash=disk,
                regenerated_hash=want,
            )
        )
    return tuple(entries)


def _read_committed_hashes(repo_root: Path, manifest: Manifest) -> dict[str, str]:
    """Hash the CURRENT on-disk content of every path the manifest owns.

 A path the manifest owns but that is absent on disk is simply omitted from the
 returned map;:func:`_diff` then classifies it relative to the regenerated set
 (absent on disk + produced by the plan => MISSING). Read-only: opens files for
 reading only.
 """
    hashes: dict[str, str] = {}
    for owned in manifest.paths:
        path = repo_root / owned
        if not path.is_file():
            continue
        hashes[owned] = content_hash(path.read_text(encoding="utf-8"))
    return hashes


def _regenerated_hashes(generation_plan: GenerationPlan) -> dict[str, str]:
    """Map each path a fresh plan would produce to the sha256 of its content.

 The manifest the generator persists is generator bookkeeping, not a generated
 artifact, so it is excluded from the drift set (it is not among ``plan.files``
 in any case — this is belt-and-braces should that ever change).
 """
    return {
        f.path: content_hash(f.content)
        for f in generation_plan.files
        if f.path != MANIFEST_RELPATH
    }


# ---------------------------------------------------------------------------
# Public API — the read-only drift check
# ---------------------------------------------------------------------------
def check_drift(
    ir: Pipeline | Path | str,
    repo_root: Path | str,
    *,
    plugins_root: Path | str | None = None,
    registry: Registry | None = None,
    settings: Mapping[str, str] | None = None,
    check_plugins: bool = True,
    clock_day: str | None = None,
) -> DriftReport:
    """Compare committed generated artifacts against a fresh in-memory render.

 Pure and **read-only**: it reads the committed manifest + owned files and
 re-runs generation via :func:`cicdgen.core.generate.plan` (which writes
 nothing). No path here opens a file for writing.

 Args:
 ir: a validated :class:`~cicdgen.ir.models.Pipeline` OR a path to a
 ``pipeline.yaml`` IR — the SAME input a prior ``generate`` used.
 repo_root: the repo whose committed artifacts are being checked.
 plugins_root / registry / settings / check_plugins / clock_day: forwarded
 verbatim to :func:`plan` so the regenerated set ``G`` is byte-identical
 to what ``generate`` would produce.

 Returns:
 A structured :class:`DriftReport` categorizing every owned path.

 Raises:
 DoctorError: if no committed manifest exists under ``repo_root`` — there is
 nothing to compare against, so the caller should run ``generate`` first.
 """
    root = Path(repo_root)
    manifest = load_manifest(root)
    if manifest is None:
        raise DoctorError(
            f"no committed manifest ({MANIFEST_RELPATH}) under {root}; "
            f"run `cicd generate` before `cicd doctor`."
        )

    committed_hashes = _read_committed_hashes(root, manifest)

    generation_plan = plan(
        ir,
        root,
        plugins_root=plugins_root,
        registry=registry,
        settings=settings,
        check_plugins=check_plugins,
        clock_day=clock_day,
    )
    regenerated_hashes = _regenerated_hashes(generation_plan)

    entries = _diff(
        committed_hashes=committed_hashes,
        regenerated_hashes=regenerated_hashes,
    )
    return DriftReport(
        repo_root=root.resolve(),
        ci_vendor=manifest.ci_vendor,
        target_platform=manifest.target_platform,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# Value-free rendering
# ---------------------------------------------------------------------------
_CATEGORY_ORDER: tuple[DriftCategory, ...] = (
    DriftCategory.MISSING,
    DriftCategory.EXTRA,
    DriftCategory.CHANGED,
    DriftCategory.IN_SYNC,
)

_CATEGORY_LABEL: dict[DriftCategory, str] = {
    DriftCategory.MISSING: "missing (would be produced, absent on disk)",
    DriftCategory.EXTRA: "extra (owned on disk, no longer produced)",
    DriftCategory.CHANGED: "changed (committed content differs from regenerated)",
    DriftCategory.IN_SYNC: "in-sync",
}


def render_drift_report(report: DriftReport, *, show_in_sync: bool = False) -> str:
    """Format a :class:`DriftReport` as a value-free, grouped-by-category report.

 Lists PATHS and CATEGORIES only; never file content. By default the
 in-sync paths are summarized by count, not listed; pass ``show_in_sync=True``
 to enumerate them too.
 """
    lines: list[str] = []
    header = (
        f"cicd doctor — drift check for {report.repo_root} "
        f"[{report.ci_vendor} × {report.target_platform}]"
    )
    lines.append(header)
    lines.append("=" * len(header))
    lines.append("")
    lines.append(
        f"{len(report.entries)} owned path(s): "
        f"{len(report.missing)} missing, {len(report.extra)} extra, "
        f"{len(report.changed)} changed, {len(report.in_sync)} in-sync."
    )
    lines.append("")

    for category in _CATEGORY_ORDER:
        if category is DriftCategory.IN_SYNC and not show_in_sync:
            continue
        group = report._by_category(category)
        if not group:
            continue
        lines.append(f"{_CATEGORY_LABEL[category]}:")
        for entry in group:
            lines.append(f"  - {entry.path}")
        lines.append("")

    if report.has_drift:
        lines.append("DRIFT DETECTED — generated files are out of date. Re-run `cicd generate`.")
    else:
        lines.append("No drift — generated files are current.")
    return "\n".join(lines).rstrip() + "\n"


# ===========================================================================
# Live wiring compare — READ-ONLY vendor/cloud queries
# ===========================================================================
class LiveStatus(StrEnum):
    """Outcome of a single live read-only wiring check.

 * ``PRESENT`` — the resource the bootstrap would create already exists.
 * ``ABSENT`` — it does not exist (a `--apply` would create it).
 * ``UNCHECKABLE`` — live access was unavailable, so the check was skipped and
 is REPORTED rather than failing the command (read-only,
 degrade gracefully).
 """

    PRESENT = "present"
    ABSENT = "absent"
    UNCHECKABLE = "uncheckable"


@dataclass(frozen=True)
class LiveFinding:
    """One read-only live-wiring observation (value-free).

 ``concern`` mirrors the bootstrap concern names (repo / oidc / branch
 protection / …); ``target`` is a NAME only (repo slug, role name, branch);
 ``detail`` references names/wiring only — never a secret value.
 """

    concern: str
    target: str
    status: LiveStatus
    detail: str = ""


@dataclass(frozen=True)
class LiveReport:
    """The structured result of a live wiring compare — value-free.

 ``available`` is False when the command could not reach any live backend (no
 executor supplied); in that case ``findings`` enumerate what could NOT be
 checked instead of the command failing hard.
 """

    available: bool
    findings: tuple[LiveFinding, ...] = field(default_factory=tuple)

    @property
    def uncheckable(self) -> tuple[LiveFinding, ...]:
        return tuple(f for f in self.findings if f.status is LiveStatus.UNCHECKABLE)

    @property
    def absent(self) -> tuple[LiveFinding, ...]:
        return tuple(f for f in self.findings if f.status is LiveStatus.ABSENT)


# A minimal read-only view of the bootstrap executor seam. We depend only on the
# DETECTION methods — never a mutating one — so a live compare writes nothing.
# Typed loosely (duck-typed) so any object exposing these reads works, including
# the real bootstrap Executor and a test fake.
def check_live_wiring(
    *,
    executor: object | None,
    repo_slug: str,
    oidc_roles: tuple[str, ...] = (),
    protected_branches: tuple[str, ...] = (),
) -> LiveReport:
    """Query vendor/cloud wiring in READ-ONLY mode through the bootstrap seam.

 Calls ONLY the executor's detection methods (``repo_exists``,
 ``oidc_trust_exists``, ``branch_protection_current``) — never a mutating one —
 so nothing is created or changed. Where ``executor`` is ``None`` (live access
 not wired up) the report says so (``available=False``) and lists each intended
 check as ``UNCHECKABLE`` rather than raising, so ``cicd doctor --live`` degrades
 gracefully instead of failing hard.

 Args:
 executor: an object exposing the bootstrap executor's READ-ONLY detection
 methods, or ``None`` when no live backend is available. Keyword-only.
 repo_slug: value-free ``owner/name`` label for reporting.
 oidc_roles: OIDC role names whose existence to probe (read-only).
 protected_branches: branch names whose protection to probe (read-only).

 Returns:
 A value-free :class:`LiveReport`.
 """
    if executor is None:
        findings: list[LiveFinding] = [
            LiveFinding(
                concern="repo",
                target=repo_slug,
                status=LiveStatus.UNCHECKABLE,
                detail="no live backend available; skipped (read-only).",
            )
        ]
        for role in oidc_roles:
            findings.append(
                LiveFinding(
                    concern="oidc",
                    target=role,
                    status=LiveStatus.UNCHECKABLE,
                    detail="no live backend available; skipped (read-only).",
                )
            )
        for branch in protected_branches:
            findings.append(
                LiveFinding(
                    concern="branch_protection",
                    target=branch,
                    status=LiveStatus.UNCHECKABLE,
                    detail="no live backend available; skipped (read-only).",
                )
            )
        return LiveReport(available=False, findings=tuple(findings))

    findings = []

    # --- repo (read-only) ---------------------------------------------------
    findings.append(
        _probe_repo(executor, repo_slug)
    )

    # --- oidc roles (read-only) --------------------------------------------
    for role in oidc_roles:
        findings.append(_probe_oidc(executor, role))

    # --- branch protection (read-only) -------------------------------------
    for branch in protected_branches:
        findings.append(_probe_branch(executor, branch))

    return LiveReport(available=True, findings=tuple(findings))


def _probe_repo(executor: object, repo_slug: str) -> LiveFinding:
    """Read-only probe of repo existence; UNCHECKABLE if the seam cannot answer."""
    check = getattr(executor, "repo_exists", None)
    if not callable(check):
        return LiveFinding(
            concern="repo",
            target=repo_slug,
            status=LiveStatus.UNCHECKABLE,
            detail="backend does not support a read-only repo check.",
        )
    exists = bool(check())
    return LiveFinding(
        concern="repo",
        target=repo_slug,
        status=LiveStatus.PRESENT if exists else LiveStatus.ABSENT,
        detail="repository present." if exists else "repository not found.",
    )


def _probe_oidc(executor: object, role_name: str) -> LiveFinding:
    """Read-only probe of an OIDC trust; UNCHECKABLE if the seam cannot answer."""
    check = getattr(executor, "oidc_trust_exists", None)
    if not callable(check):
        return LiveFinding(
            concern="oidc",
            target=role_name,
            status=LiveStatus.UNCHECKABLE,
            detail="backend does not support a read-only OIDC check.",
        )
    exists = bool(check("oidc", role_name))
    return LiveFinding(
        concern="oidc",
        target=role_name,
        status=LiveStatus.PRESENT if exists else LiveStatus.ABSENT,
        detail="OIDC trust present." if exists else "OIDC trust not found.",
    )


def _probe_branch(executor: object, branch: str) -> LiveFinding:
    """Read-only probe of branch protection; UNCHECKABLE if the seam cannot answer."""
    check = getattr(executor, "branch_protection_current", None)
    if not callable(check):
        return LiveFinding(
            concern="branch_protection",
            target=branch,
            status=LiveStatus.UNCHECKABLE,
            detail="backend does not support a read-only branch-protection check.",
        )
    current = check(branch)
    present = current is not None
    return LiveFinding(
        concern="branch_protection",
        target=branch,
        status=LiveStatus.PRESENT if present else LiveStatus.ABSENT,
        detail="branch protection present." if present else "branch protection not set.",
    )


def render_live_report(report: LiveReport) -> str:
    """Format a :class:`LiveReport` as a value-free, read-only compare report."""
    lines: list[str] = []
    header = "cicd doctor --live — read-only wiring compare"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append("")
    if not report.available:
        lines.append(
            "Live backend unavailable — nothing was mutated. "
            "The following checks were skipped and remain UNVERIFIED:"
        )
        lines.append("")
    for f in report.findings:
        lines.append(f"  - [{f.status}] ({f.concern}) {f.target}: {f.detail}")
    return "\n".join(lines).rstrip() + "\n"
