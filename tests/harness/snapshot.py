"""Snapshot test harness — render a plugin for a fixture IR, compare to a committed
snapshot (task 21.1, design §11.8).

Goal
----
A reusable harness so that adding a new ``target_platform`` plugin (the §11.9
cross-cloud matrix) comes with regression coverage for near-zero cost: author the
plugin, drop a fixture under ``tests/fixtures/ir/``, run the harness once with
``update=True`` to record the first snapshot, review that snapshot in the PR diff.
Thereafter the committed snapshot guards the plugin.

Two public functions (the shapes design §11.8 specifies, plus an injectable
``ci_vendor`` so a fixture can be rendered against any CI axis without hard-coding
one):

    render_plugin(target_platform, fixture_ir, *, ci_vendor=..., registry=None)
        -> dict[str, str]
            Render what a plugin WOULD emit for a fixture IR, in memory. Reuses the
            generator core's ``plan()`` (dry-run) path so NOTHING is written to any
            repo (R9.2 spirit). Returns a mapping of generated repo-relative path ->
            file content.

    assert_snapshot(name, rendered, *, update=False) -> None
            Compare ``rendered`` against the committed snapshot under
            ``tests/snapshots/<name>/``. When ``update`` is True (or the env flag
            ``UPDATE_SNAPSHOTS=1`` is set) the snapshot is (re)written; otherwise it
            asserts byte-equality and fails with a readable diff naming the drifting
            path(s).

Design choices that keep snapshots trustworthy
----------------------------------------------
* **Writes nothing to the repo during render.** ``render_plugin`` calls ``plan()``
  which is side-effect-free; the only files it returns are the plugin-produced
  artifacts (never the ``.cicd/generated.manifest.json`` the core would persist on a
  real run — a manifest is generator bookkeeping, not plugin output, and excluding
  it keeps the snapshot focused on what the plugin actually emits).
* **Deterministic & secret-free (R5.3 / R6).** ``plan()`` already sorts files by
  path and runs the secrets-by-reference guard; the snapshot stores content
  verbatim. The on-disk snapshot layout mirrors the generated path tree under
  ``files/`` and records a sorted ``manifest.json`` so a diff reads naturally.
* **Vendor-neutral fixtures.** A fixture IR names no load-bearing axis: the harness
  overrides ``target_platform`` (and picks a matching ``cloud.provider``) and
  ``ci_vendor`` per render, so one fixture conforms every plugin in the matrix.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import yaml

from cicdgen.core.generate import plan
from cicdgen.ir import Pipeline
from cicdgen.plugins.loader import Registry

# <repo>/tests/harness/snapshot.py -> <repo>; the real plugin tree lives at <repo>/plugins.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS_ROOT = _REPO_ROOT / "plugins"

#: Where fixtures and committed snapshots live (design §11.8).
FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "ir"
SNAPSHOTS_DIR = _REPO_ROOT / "tests" / "snapshots"

#: Env flag that flips every ``assert_snapshot`` into record/refresh mode.
UPDATE_ENV_FLAG = "UPDATE_SNAPSHOTS"

#: The generator-bookkeeping file the core persists on a REAL run. It is never a
#: plugin artifact, so it is excluded from a render (it is not produced by ``plan``
#: in any case — this is a belt-and-braces guard should that ever change).
_MANIFEST_RELPATH = ".cicd/generated.manifest.json"

#: Default CI axis a render uses when the caller does not pin one. ``github`` is the
#: baseline vendor present from task 6; nothing about the snapshot harness depends on
#: this particular value — it is only a default, overridable per call.
DEFAULT_CI_VENDOR = "github"

#: Sensible ``cloud.provider`` per target so the rendered IR is coherent (the IR does
#: not cross-validate provider vs. target, but a matching provider keeps the snapshot
#: faithful to how a real client would configure the pair). Data only — no branching
#: logic lives in the core; this is a test-side convenience table. Unknown targets
#: fall back to ``aws`` (a safe generic default) so a NEW matrix plugin renders
#: without needing a harness edit — only a fixture.
_CLOUD_PROVIDER_FOR_TARGET: dict[str, str] = {
    "aws-ecs": "aws",
    "kubernetes": "aws",
    "databricks": "azure",
    "azure-functions": "azure",
    "azure-aks": "azure",
    "azure-synapse": "azure",
    "azure-data-factory": "azure",
    "azure-container-apps": "azure",
    "gcp-cloud-run": "gcp",
    "gcp-gke": "gcp",
    "gcp-dataproc": "gcp",
    "gcp-dataflow": "gcp",
}
_DEFAULT_CLOUD_PROVIDER = "aws"


def cloud_provider_for_target(target_platform: str) -> str:
    """The sensible ``cloud.provider`` for a target (data-only; defaults to ``aws``).

    Exposed so a conformance test can build a coherent IR for a target without
    reaching into harness internals. A target not in the table falls back to the
    generic default, so a NEW matrix plugin needs no harness edit.
    """
    return _CLOUD_PROVIDER_FOR_TARGET.get(target_platform, _DEFAULT_CLOUD_PROVIDER)


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------
def _resolve_fixture(fixture_ir: Path | str) -> Path:
    """Resolve a fixture path: an absolute/relative path, or a bare fixture name."""
    candidate = Path(fixture_ir)
    if candidate.suffix in {".yaml", ".yml"} and candidate.exists():
        return candidate
    # Bare name (e.g. "minimal") -> tests/fixtures/ir/<name>.yaml
    named = FIXTURES_DIR / f"{candidate.name}.yaml"
    if named.exists():
        return named
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f"fixture IR not found: {fixture_ir!r} (looked for {candidate} and {named})"
    )


def _load_fixture_doc(fixture_ir: Path | str) -> dict[str, object]:
    """Parse a fixture YAML into a plain dict (not yet a validated Pipeline)."""
    path = _resolve_fixture(fixture_ir)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"fixture IR {path} did not parse to a mapping")
    return data


def _apply_axes(
    doc: dict[str, object], *, ci_vendor: str, target_platform: str
) -> dict[str, object]:
    """Return a copy of ``doc`` with both axes (and a matching cloud provider) set.

    Pure data transform: the fixture declares the shape; the harness pins the axes
    for this particular render so one fixture conforms every plugin. The cloud
    provider is aligned to the target via :data:`_CLOUD_PROVIDER_FOR_TARGET` so the
    rendered IR is coherent; the region/auth from the fixture (if any) are kept.
    """
    out = dict(doc)
    out["ci_vendor"] = ci_vendor
    out["target_platform"] = target_platform

    provider = _CLOUD_PROVIDER_FOR_TARGET.get(target_platform, _DEFAULT_CLOUD_PROVIDER)
    existing_cloud = out.get("cloud")
    cloud: dict[str, object] = dict(existing_cloud) if isinstance(existing_cloud, dict) else {}
    cloud.setdefault("auth", "oidc")
    cloud["provider"] = provider
    out["cloud"] = cloud
    return out


# ---------------------------------------------------------------------------
# Public: render_plugin
# ---------------------------------------------------------------------------
def render_plugin(
    target_platform: str,
    fixture_ir: Path | str,
    *,
    ci_vendor: str = DEFAULT_CI_VENDOR,
    registry: Registry | None = None,
) -> dict[str, str]:
    """Return ``{relative_path: content}`` a plugin would emit for a fixture IR.

    Pure / side-effect-free: reuses the generator core's in-memory ``plan()``
    (dry-run) path, so NOTHING is written to any repo (design §11.8, R9.2 spirit).
    The fixture's axes are overridden with ``target_platform`` / ``ci_vendor`` (and a
    matching ``cloud.provider``) so a single vendor-neutral fixture renders against
    any plugin in the matrix.

    Args:
        target_platform: the IR ``target_platform`` value to render (e.g. ``aws-ecs``).
        fixture_ir: a fixture YAML path, or a bare fixture name resolved under
            ``tests/fixtures/ir/``.
        ci_vendor: the IR ``ci_vendor`` to render against (defaults to
            :data:`DEFAULT_CI_VENDOR`). The snapshot harness itself is CI-axis
            agnostic; pinning one just makes the render concrete.
        registry: optional in-memory plugin registry to bypass the on-disk tree
            (tests may inject a fake plugin set). When ``None`` the real
            ``<repo>/plugins`` tree is used.

    Returns:
        A mapping of generated repo-relative POSIX path -> file content, sorted by
        path, excluding the generator-bookkeeping manifest.
    """
    doc = _load_fixture_doc(fixture_ir)
    doc = _apply_axes(doc, ci_vendor=ci_vendor, target_platform=target_platform)
    pipeline = Pipeline.model_validate(doc)

    # A throwaway root the plan never writes to (plan() is side-effect-free). Using
    # the real plugins_root means the on-disk plugins actually render; a tmp repo
    # root is only a label here since nothing is written.
    plan_root = _REPO_ROOT / "tests" / ".render-root"

    plan_result = plan(
        pipeline,
        plan_root,
        plugins_root=None if registry is not None else _PLUGINS_ROOT,
        registry=registry,
        # Plugins live in memory when a registry is injected; otherwise they are on
        # the real tree. Either way plan() writes nothing.
        check_plugins=False,
    )

    rendered: dict[str, str] = {
        f.path: f.content
        for f in plan_result.files
        if f.path != _MANIFEST_RELPATH
    }
    # Return sorted by path for a stable, reviewable mapping (R5.3).
    return {path: rendered[path] for path in sorted(rendered)}


# ---------------------------------------------------------------------------
# Snapshot storage
# ---------------------------------------------------------------------------
def _snapshot_dir(name: str) -> Path:
    return SNAPSHOTS_DIR / name


def _manifest_path(name: str) -> Path:
    return _snapshot_dir(name) / "manifest.json"


def _artifact_path(name: str, rel_path: str) -> Path:
    """On-disk location of one snapshotted artifact (mirrors the generated tree)."""
    return _snapshot_dir(name) / "files" / rel_path


def _update_requested(update: bool) -> bool:
    """True when an update was asked for explicitly or via the env flag."""
    if update:
        return True
    return os.environ.get(UPDATE_ENV_FLAG, "").strip() in {"1", "true", "yes", "on"}


def _write_snapshot(name: str, rendered: Mapping[str, str]) -> None:
    """(Re)write the committed snapshot for ``name`` from ``rendered`` (sorted)."""
    snap_dir = _snapshot_dir(name)
    files_dir = snap_dir / "files"

    # Start clean so a removed artifact does not linger in the snapshot. Deleting
    # the FILES is what matters (a stale artifact must not survive a refresh);
    # removing now-empty directories is best-effort. On Windows — notably when the
    # repo lives under OneDrive — a directory handle may be transiently held and
    # ``rmdir`` raises PermissionError; that is harmless here because the files are
    # gone and the tree is rewritten immediately below, so we tolerate it.
    if snap_dir.exists():
        for existing in sorted(snap_dir.rglob("*"), reverse=True):
            try:
                if existing.is_file():
                    existing.unlink()
                elif existing.is_dir():
                    existing.rmdir()
            except (PermissionError, OSError):
                # Best-effort cleanup: a locked/non-empty dir is fine to leave; the
                # rewrite below restores the authoritative contents.
                pass
    files_dir.mkdir(parents=True, exist_ok=True)

    for rel_path in sorted(rendered):
        target = _artifact_path(name, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered[rel_path], encoding="utf-8", newline="")

    manifest = {"paths": sorted(rendered)}
    _manifest_path(name).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="",
    )


def _read_snapshot(name: str) -> dict[str, str]:
    """Load the committed snapshot for ``name`` as ``{relative_path: content}``."""
    manifest_path = _manifest_path(name)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = manifest.get("paths", [])
    out: dict[str, str] = {}
    for rel_path in paths:
        out[rel_path] = _artifact_path(name, rel_path).read_text(encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Public: assert_snapshot
# ---------------------------------------------------------------------------
def _diff_message(name: str, expected: Mapping[str, str], actual: Mapping[str, str]) -> str:
    """A readable failure message naming every drifting path."""
    exp_paths = set(expected)
    act_paths = set(actual)

    missing = sorted(exp_paths - act_paths)  # in snapshot, not in fresh render
    extra = sorted(act_paths - exp_paths)    # in fresh render, not in snapshot
    changed = sorted(
        p for p in (exp_paths & act_paths) if expected[p] != actual[p]
    )

    lines: list[str] = [
        f"snapshot '{name}' does not match a fresh render.",
        "Run with UPDATE_SNAPSHOTS=1 (or update=True) to re-record it and review the diff.",
    ]
    if missing:
        lines.append(f"  paths in snapshot but NOT rendered: {missing}")
    if extra:
        lines.append(f"  paths rendered but NOT in snapshot: {extra}")
    if changed:
        lines.append(f"  paths whose content drifted: {changed}")
        # Show a compact unified diff for the first changed path to aid debugging.
        import difflib

        first = changed[0]
        diff = difflib.unified_diff(
            expected[first].splitlines(keepends=False),
            actual[first].splitlines(keepends=False),
            fromfile=f"snapshot/{first}",
            tofile=f"rendered/{first}",
            lineterm="",
        )
        lines.append("  --- first drifting artifact ---")
        lines.extend(f"    {line}" for line in list(diff)[:200])
    return "\n".join(lines)


def assert_snapshot(
    name: str,
    rendered: Mapping[str, str],
    *,
    update: bool = False,
) -> None:
    """Compare ``rendered`` against ``tests/snapshots/<name>/``; record on update.

    Args:
        name: the snapshot name (one directory per name under
            ``tests/snapshots/``). Conventionally ``<ci_vendor>__<target>__<fixture>``
            or ``<target>__<fixture>`` so the matrix reads clearly in a diff.
        rendered: the ``{relative_path: content}`` mapping from :func:`render_plugin`.
        update: force (re)writing the snapshot. Also triggered by the env flag
            ``UPDATE_SNAPSHOTS=1`` so a reviewer can refresh the whole matrix at once.

    Raises:
        AssertionError: when not updating and the committed snapshot differs from the
            fresh render, with a message naming each drifting path.
    """
    if _update_requested(update):
        _write_snapshot(name, rendered)
        return

    manifest_path = _manifest_path(name)
    if not manifest_path.is_file():
        raise AssertionError(
            f"no committed snapshot for '{name}' at {manifest_path}. "
            f"Record it first with UPDATE_SNAPSHOTS=1 (or update=True) and review "
            f"the diff in the PR."
        )

    expected = _read_snapshot(name)
    actual = {path: rendered[path] for path in sorted(rendered)}

    if expected != actual:
        raise AssertionError(_diff_message(name, expected, actual))
