"""Generator core orchestration — validate → dispatch → write → manifest.

The "render → write + manifest" entry point. It ties the pieces together into one
re-runnable, deterministic pass:

 1. **Validate** the IR (``cicdgen.ir.load_and_validate_ir``) → a defaults-
 resolved :class:`Pipeline`.
 2. **Select + render** by dispatching to the two named plugins
 (``cicdgen.plugins.dispatch``) — the plugins ARE the vendor pack and they
 render their own files.
 3. **Write** every :class:`GeneratedFile` under the target repo root, creating
 parent dirs and honoring the executable bit where the OS supports it.
 4. **Manifest** every file it owns into ``.cicd/generated.manifest.json`` so a
 later run regenerates cleanly and removes stale files on an axis swap.

Two independent plugin axes, zero vendor/platform knowledge here: this module
reads only ``pipeline.ci_vendor`` / ``pipeline.target_platform`` indirectly via
dispatch and never branches on their values.

Determinism
-----------
Identical IR + repo → byte-identical output, guaranteed by:

 * writing files sorted by path,
 * writing exactly the plugin-produced content (no re-formatting),
 * a deterministic manifest (sorted keys/paths, no wall-clock timestamp — see
:mod:`cicdgen.core.manifest`),
 * not embedding timestamps in anything the generator authors.

Secrets by reference
--------------------
The core never injects secret *values*; it writes exactly what the plugins
produced, and plugins emit secret *references* only. As a lightweight safety net
(opt-out) the writer scans generated content for a few obvious secret-value
leakage patterns and refuses to write if one is found, so a mis-behaving plugin
cannot slip a literal credential into a committed file.

Dry-run / plan mode
-------------------
:func:`plan` (and ``generate(..., dry_run=True)``) returns the exact set of files
that WOULD be written — with their content and ownership — and touches nothing on
disk, matching the dry-run-by-default safety posture.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cicdgen.core.manifest import MANIFEST_RELPATH, Manifest
from cicdgen.core.migration import AxisChange, detect_axis_change
from cicdgen.core.policy import enforce_policies, load_policy_doc
from cicdgen.core.secrets_guard import SecretLeak, enforce_secrets_by_reference
from cicdgen.ir import load_and_validate_ir
from cicdgen.ir.models import Pipeline
from cicdgen.plugins.base import GeneratedFile
from cicdgen.plugins.dispatch import DispatchResult, dispatch
from cicdgen.plugins.loader import Registry


class GenerationError(RuntimeError):
    """Raised when generation cannot proceed safely (path escape, secret leak)."""


# ---------------------------------------------------------------------------
# Plan / result data
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GenerationPlan:
    """The set of files generation WOULD write, plus the manifest — no side effects.

 Returned by :func:`plan` and by :func:`generate` in dry-run mode. ``files`` is
 sorted by path (determinism); ``manifest`` is the manifest that would be
 written; ``removed`` lists paths a previous run owned that this run no longer
 owns (cleaned up on a real run).
 """

    repo_root: Path
    ci_vendor: str
    target_platform: str
    files: tuple[GeneratedFile, ...]
    manifest: Manifest
    removed: tuple[str, ...] = ()
    axis_change: AxisChange | None = None
    dispatch_result: DispatchResult | None = field(default=None, repr=False)

    @property
    def paths(self) -> tuple[str, ...]:
        """Every file path that would be written (sorted), incl. the manifest."""
        return tuple(f.path for f in self.files)


@dataclass(frozen=True)
class GenerationResult:
    """The outcome of a real (non-dry-run) generation pass.

 ``written`` are the repo-relative paths actually written (sorted, incl. the
 manifest). ``removed`` are stale paths deleted because a previous manifest
 owned them but this run does not. ``manifest`` is what was persisted.
 ``axis_change`` classifies the run as an axis swap vs. the prior manifest
 (``None`` on a first run; ``is_axis_change=False`` on a same-axes regenerate).
 """

    repo_root: Path
    ci_vendor: str
    target_platform: str
    written: tuple[str, ...]
    removed: tuple[str, ...]
    manifest: Manifest
    dispatch_result: DispatchResult = field(repr=False)
    axis_change: AxisChange | None = None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _coerce_pipeline(
    ir: Pipeline | Path | str,
    *,
    check_plugins: bool,
    plugins_root: Path | str | None,
    registry: Registry | None,
) -> Pipeline:
    """Accept a ready Pipeline or an IR path and return a validated Pipeline."""
    if isinstance(ir, Pipeline):
        return ir
    # When a registry is injected (tests / agent), the on-disk plugin existence
    # check is meaningless — the plugins live in memory. Skip it in that case.
    do_check = check_plugins and registry is None
    return load_and_validate_ir(
        ir,
        check_plugins=do_check,
        repo_root=plugins_root,
    )


def _resolve_under_root(repo_root: Path, rel_path: str) -> Path:
    """Resolve a repo-relative path, refusing to escape the repo root.

 Guards against a malicious/buggy plugin emitting ``../`` or an absolute path
 that would write outside the target repo (safety; keeps generation contained
 to the repo being generated into).
 """
    candidate = (repo_root / rel_path).resolve()
    root = repo_root.resolve()
    if candidate != root and root not in candidate.parents:
        raise GenerationError(
            f"generated path '{rel_path}' resolves outside the repo root "
            f"({candidate}); refusing to write."
        )
    return candidate


def _build_plan(
    pipeline: Pipeline,
    repo_root: Path,
    *,
    plugins_root: Path | str | None,
    registry: Registry | None,
    settings: Mapping[str, str] | None,
    scan_secrets: bool,
    secret_values: Mapping[str, str] | None = None,
    clock_day: str | None = None,
    manifest_root: Path | None = None,
) -> GenerationPlan:
    """Validate → policy gate → dispatch → assemble the (side-effect-free) plan.

 ``manifest_root`` is where the PRIOR manifest is read from for stale-file and
 axis-swap detection — the OUTPUT root when source/output are separated. It
 defaults to ``repo_root`` so existing callers are unchanged.
 """
    # --- policy gate: BEFORE any render / dispatch ---------------------------
    # Load the committed.cicd/policies.yaml if present and fail fast, listing
    # EVERY violation in one pass. Absent file => no-op.
    # Policies read ONLY the vendor-neutral IR; the temporal rule's day is
    # injected via clock_day and is never read from the wall clock here.
    enforce_policies(
        pipeline, load_policy_doc(repo_root), clock_day=clock_day
    )

    result = dispatch(
        pipeline,
        plugins_root=plugins_root,
        registry=registry,
        settings=settings,
    )

    # Deterministic order: sort every generated file by path.
    files = tuple(sorted(result.all_files(), key=lambda f: f.path))

    # Secrets-by-reference gate: the exact-value check runs whenever actual values
    # are supplied; the heuristic net runs when ``scan_secrets`` is on. A leak is
    # re-raised as GenerationError with the same value-free message.
    try:
        enforce_secrets_by_reference(
            files, secret_values=secret_values, heuristic=scan_secrets
        )
    except SecretLeak as exc:
        raise GenerationError(str(exc)) from exc

    manifest = Manifest.build(
        ci_vendor=result.ci_vendor,
        target_platform=result.target_platform,
        files=files,
    )

    # Load the prior manifest once and reuse it for BOTH stale-file detection
    # and axis-swap classification. Reading it a single time keeps
    # the two derived facts consistent with each other. It is read from the OUTPUT
    # root (manifest_root) where it was written, which equals repo_root unless
    # source/output are separated.
    prev_manifest = load_manifest(manifest_root if manifest_root is not None else repo_root)

    # What a prior run owned that this run no longer owns -> candidates for removal.
    removed = _stale_paths(prev_manifest, new_paths=set(manifest.paths))

    # Classify this regenerate as an axis swap (ci_vendor and/or target_platform
    # change) vs. the prior manifest. None on a first run (nothing to migrate
    # from); an AxisChange with is_axis_change=False on a same-axes regenerate.
    axis_change = detect_axis_change(
        prev_manifest,
        new_ci_vendor=result.ci_vendor,
        new_target_platform=result.target_platform,
    )

    return GenerationPlan(
        repo_root=repo_root.resolve(),
        ci_vendor=result.ci_vendor,
        target_platform=result.target_platform,
        files=files,
        manifest=manifest,
        removed=removed,
        axis_change=axis_change,
        dispatch_result=result,
    )


def _stale_paths(
    prev_manifest: Manifest | None, *, new_paths: set[str]
) -> tuple[str, ...]:
    """Paths the previous manifest owned but the new run does not (sorted).

 Takes the already-loaded prior manifest so the caller can reuse the single
 manifest read for axis-swap classification too.
 """
    if prev_manifest is None:
        return ()
    stale = sorted(set(prev_manifest.paths) - new_paths)
    return tuple(stale)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def load_manifest(repo_root: Path | str) -> Manifest | None:
    """Load the existing ``.cicd/generated.manifest.json`` under ``repo_root``.

 Returns ``None`` when no manifest exists yet (first run) or it is unreadable
 — callers treat that as "the generator owns nothing here yet".
 """
    path = Path(repo_root) / MANIFEST_RELPATH
    if not path.is_file():
        return None
    try:
        return Manifest.from_json_str(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def plan(
    ir: Pipeline | Path | str,
    repo_root: Path | str,
    *,
    plugins_root: Path | str | None = None,
    registry: Registry | None = None,
    settings: Mapping[str, str] | None = None,
    check_plugins: bool = True,
    scan_secrets: bool = True,
    secret_values: Mapping[str, str] | None = None,
    clock_day: str | None = None,
) -> GenerationPlan:
    """Compute what generation WOULD do, writing nothing (dry-run / plan mode).

 Args:
 ir: a validated :class:`Pipeline` OR a path to a ``pipeline.yaml`` IR.
 repo_root: the target repo to generate into.
 plugins_root: root containing ``ci/`` and ``target/`` plugin folders
 (defaults to the product's ``plugins/``); injectable for tests.
 registry: optional in-memory ``(family, name) -> plugin`` override that
 bypasses the filesystem.
 settings: resolved, non-secret knobs forwarded into every verb context.
 check_plugins: validate that the named plugins exist on disk when loading
 an IR from a path (ignored when a ``registry`` is injected).
 scan_secrets: run the lightweight secret-value leak heuristic.
 secret_values: optional ACTUAL secret ``name -> value`` map (e.g. from a
 loaded ``LocalConfig``/``SecretStore``). When provided, the strongest
 exact-value leak check runs — any real value appearing verbatim in a
 generated file is refused, naming the secret KEY + file PATH only,
 never the value.
 clock_day: the INJECTED day-of-week for the policy gate's temporal rules
 (lower-case full weekday name, e.g. ``"friday"``). The core NEVER
 reads the wall clock; a caller that wants the temporal policy
 evaluated passes the day explicitly. ``None`` => no temporal rule
 fires.

 Returns:
 A :class:`GenerationPlan` listing every file that would be written (sorted
 by path), the manifest that would be persisted, and any stale paths a
 prior run owned that would be removed.
 """
    root = Path(repo_root)
    pipeline = _coerce_pipeline(
        ir, check_plugins=check_plugins, plugins_root=plugins_root, registry=registry
    )
    return _build_plan(
        pipeline,
        root,
        plugins_root=plugins_root,
        registry=registry,
        settings=settings,
        scan_secrets=scan_secrets,
        secret_values=secret_values,
        clock_day=clock_day,
    )


def generate(
    ir: Pipeline | Path | str,
    repo_root: Path | str,
    *,
    plugins_root: Path | str | None = None,
    registry: Registry | None = None,
    settings: Mapping[str, str] | None = None,
    check_plugins: bool = True,
    scan_secrets: bool = True,
    secret_values: Mapping[str, str] | None = None,
    clock_day: str | None = None,
    dry_run: bool = False,
    output_root: Path | str | None = None,
) -> GenerationResult | GenerationPlan:
    """Validate the IR, render via plugins, and write files + the manifest.

 The single entry point for the "render → write + manifest" pass. On a real
 run it writes every generated file under the OUTPUT root (creating dirs,
 honoring the ``+x`` bit where the OS supports it), removes any stale file a
 previous manifest owned but this run does not, and persists a deterministic
 ``.cicd/generated.manifest.json``.

 Source vs. output separation (``output_root``)
 ----------------------------------------------
 ``repo_root`` is the SOURCE root — where the IR and the developer's code live,
 and the base that ``settings['source_dir']`` resolves against. ``output_root``,
 when given, is where every generated file + the manifest are WRITTEN instead.
 This keeps a client repo's hand-authored source (``src/<service>/``) physically
 separate from the machine-generated artifacts (``generated/<service>/``) so the
 two never get confused. When ``output_root`` is ``None`` (the default) output is
 written under ``repo_root``.

 Re-running with the same IR + repo is idempotent and byte-identical: the same
 files with the same content and the same manifest.

 Args:
 ir / repo_root / plugins_root / registry / settings / check_plugins /
 scan_secrets / secret_values / clock_day: see :func:`plan`.
 dry_run: when True, write nothing and return the :class:`GenerationPlan`
 (same as calling :func:`plan`), matching the dry-run-by-default
 safety posture for destructive-ish actions.

 Returns:
 A :class:`GenerationResult` on a real run, or a :class:`GenerationPlan`
 when ``dry_run`` is True.
 """
    root = Path(repo_root)
    # Where generated files + the manifest are WRITTEN. Defaults to the source
    # root; a separate output_root keeps machine output out of the hand-authored
    # source tree.
    out_root = Path(output_root) if output_root is not None else root
    pipeline = _coerce_pipeline(
        ir, check_plugins=check_plugins, plugins_root=plugins_root, registry=registry
    )
    the_plan = _build_plan(
        pipeline,
        root,
        plugins_root=plugins_root,
        registry=registry,
        settings=settings,
        scan_secrets=scan_secrets,
        secret_values=secret_values,
        clock_day=clock_day,
        manifest_root=out_root,
    )

    if dry_run:
        return the_plan

    # --- write phase (deterministic order already guaranteed by the plan) ----
    # All writes land under the OUTPUT root; the path-escape guard is anchored
    # there too so a generated path can never escape the output tree.
    written: list[str] = []
    for f in the_plan.files:
        target = _resolve_under_root(out_root, f.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write bytes we control: exact content, UTF-8, no re-formatting.
        target.write_text(f.content, encoding="utf-8", newline="")
        _apply_executable_bit(target, executable=f.executable)
        written.append(f.path)

    # --- clean up stale files a previous manifest owned ----------------------
    removed: list[str] = []
    for rel in the_plan.removed:
        stale = _resolve_under_root(out_root, rel)
        if stale.is_file():
            stale.unlink()
            removed.append(rel)

    # --- persist the manifest last (so it reflects the final state) ----------
    manifest_path = _resolve_under_root(out_root, MANIFEST_RELPATH)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(the_plan.manifest.to_json_str(), encoding="utf-8", newline="")

    written_all = tuple(sorted({*written, MANIFEST_RELPATH}))

    assert the_plan.dispatch_result is not None  # always set by _build_plan
    return GenerationResult(
        repo_root=root.resolve(),
        ci_vendor=the_plan.ci_vendor,
        target_platform=the_plan.target_platform,
        written=written_all,
        removed=tuple(removed),
        manifest=the_plan.manifest,
        axis_change=the_plan.axis_change,
        dispatch_result=the_plan.dispatch_result,
    )


def _apply_executable_bit(path: Path, *, executable: bool) -> None:
    """Set/clear the owner+group+other execute bits where the OS supports it.

 On Windows the execute bit is not meaningful; ``os.chmod`` there is a no-op
 for these flags, so we guard to keep behaviour predictable and errors quiet.
 """
    if not executable:
        return
    if os.name != "posix":
        return
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
