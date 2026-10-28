"""Focused determinism suite — same IR + repo → byte-identical output (task 5.3, R5.3).

This complements the lighter determinism checks in ``test_core_generate.py``
(``test_rerun_is_byte_identical`` / ``test_manifest_serialization_is_deterministic``)
with a thorough proof that generation is a *pure function of the IR + repo*:

    1. Two runs into TWO DIFFERENT fresh repo dirs (same IR + same injected
       registry) are byte-for-byte identical — every file AND the manifest JSON.
    2. Repeated ``generate`` into the SAME dir is idempotent + byte-identical.
    3. Manifest + on-disk bytes are independent of the ORDER a plugin emits files
       (sort stability): a plugin that reverses/shuffles its output yields the
       same result.
    4. ``plan`` is deterministic: equal file tuples, equal manifest JSON, equal
       content hashes across two identical calls.
    5. No wall-clock leaks: dict/set ordering across many files doesn't leak, and
       two runs separated by a real sleep stay identical (no timestamp sneaks in).
    6. A single SHA-256 over all sorted ``(path, bytes)`` pairs is stable across
       runs (and across two different repo dirs).

Everything runs through an *injected* in-memory registry with real fake plugins
(``CIVendorPlugin`` / ``TargetPlatformPlugin`` subclasses), mirroring
``test_core_generate.py`` — no mocks, no filesystem plugin tree.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from cicdgen.core import (
    MANIFEST_RELPATH,
    GenerationResult,
    content_hash,
    generate,
    plan,
)
from cicdgen.ir import Pipeline
from cicdgen.plugins import (
    Capability,
    CIVendorPlugin,
    GeneratedFile,
    IaCArtifacts,
    PluginFamily,
    StageGraph,
    TargetPlatformPlugin,
    VerbContext,
    VerbResult,
)

#: Full-capability stand-in set so the §11.2 gate (task 17) passes for these IRs.
_ALL_CAPS = frozenset(Capability)


# ---------------------------------------------------------------------------
# IR helper
# ---------------------------------------------------------------------------
def _pipeline(
    *,
    ci_vendor: str = "github",
    target_platform: str = "aws-ecs",
    stage_ids: tuple[str, ...] = ("test", "build"),
) -> Pipeline:
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": ci_vendor,
            "target_platform": target_platform,
            "project": {"name": "demo"},
            "environments": [{"name": "dev"}],
            "stages": [{"id": sid} for sid in stage_ids],
        }
    )


# ---------------------------------------------------------------------------
# Fake plugins (in-memory registry) — real subclasses, no mocks
# ---------------------------------------------------------------------------
class _FakeCI(CIVendorPlugin):
    name = "github"

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        stage_ids = ",".join(s.id for s in graph.stages)
        return (
            GeneratedFile(
                path=".github/workflows/ci.yml",
                content=f"# ci\nstages: {stage_ids}\n",
            ),
            GeneratedFile(path="scripts/run.sh", content="#!/bin/sh\necho hi\n", executable=True),
        )


class _FakeTarget(TargetPlatformPlugin):
    name = "aws-ecs"
    capabilities = _ALL_CAPS

    def _verb(self, ctx: VerbContext) -> VerbResult:
        return VerbResult(
            commands=(ctx.verb.value,),
            files=(GeneratedFile(path=f"verb/{ctx.verb.value}.sh", content=ctx.verb.value + "\n"),),
        )

    lint = _verb
    test = _verb
    build = _verb
    package = _verb
    deploy = _verb
    verify = _verb

    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        return IaCArtifacts(
            files=(GeneratedFile(path="iac/main.tf", content="# terraform\n"),),
            summary="demo infra",
        )


class _ReversedEmitCI(CIVendorPlugin):
    """Emits the SAME file set as ``_FakeCI`` but in reversed order.

    Used to prove the manifest + on-disk bytes are independent of plugin emission
    order (sort stability — R5.3).
    """

    name = "github"

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        stage_ids = ",".join(s.id for s in graph.stages)
        files = (
            GeneratedFile(
                path=".github/workflows/ci.yml",
                content=f"# ci\nstages: {stage_ids}\n",
            ),
            GeneratedFile(path="scripts/run.sh", content="#!/bin/sh\necho hi\n", executable=True),
        )
        return tuple(reversed(files))


class _ManyFilesCI(CIVendorPlugin):
    """Emits many files whose keys would hash-order differently than sorted order.

    The paths are intentionally not pre-sorted and chosen so a dict/set iteration
    order (hash order) would differ from lexicographic order. Proves no hash-order
    leaks into the output (R5.3).
    """

    name = "github"

    #: Deliberately out of sorted order; mix of depths + sibling names.
    _paths = (
        "zeta/one.yml",
        "alpha/10.yml",
        "alpha/2.yml",
        ".github/workflows/ci.yml",
        "m/x.yml",
        "m/a.yml",
        "b.yml",
        "alpha/1.yml",
        "nested/deep/leaf.yml",
        "nested/deep/another.yml",
    )

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        return tuple(
            GeneratedFile(path=p, content=f"# {p}\n") for p in self._paths
        )


def _registry(ci: CIVendorPlugin | None = None, target: TargetPlatformPlugin | None = None) -> dict:
    return {
        (PluginFamily.CI_VENDOR, "github"): ci or _FakeCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): target or _FakeTarget(),
    }


# ---------------------------------------------------------------------------
# Byte-level snapshot helpers
# ---------------------------------------------------------------------------
def _byte_snapshot(repo_root: Path) -> dict[str, bytes]:
    """Map repo-relative POSIX path -> raw bytes for every file under the root."""
    snapshot: dict[str, bytes] = {}
    for p in sorted(repo_root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(repo_root).as_posix()
            snapshot[rel] = p.read_bytes()
    return snapshot


def _run_digest(repo_root: Path) -> str:
    """A single SHA-256 over all sorted ``(path, bytes)`` pairs for a run.

    A stable digest across runs is a compact, total proof of byte-level
    determinism (R5.3). The path is included so a renamed-but-same-content file
    would still change the digest.
    """
    h = hashlib.sha256()
    for rel in sorted(_byte_snapshot(repo_root)):
        data = (repo_root / rel).read_bytes()
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


# ===========================================================================
# 1. Two separate runs into TWO DIFFERENT fresh repos are byte-identical
# ===========================================================================
def test_two_fresh_repos_are_byte_identical(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    ir = _pipeline()
    generate(ir, repo_a, registry=_registry())
    generate(_pipeline(), repo_b, registry=_registry())

    snap_a = _byte_snapshot(repo_a)
    snap_b = _byte_snapshot(repo_b)

    assert snap_a.keys() == snap_b.keys()
    for rel in snap_a:
        assert snap_a[rel] == snap_b[rel], f"{rel} differs between two fresh repos"

    # the manifest JSON itself is byte-identical (compared as raw bytes)
    assert snap_a[MANIFEST_RELPATH] == snap_b[MANIFEST_RELPATH]


# ===========================================================================
# 2. Repeated generate into the SAME repo is idempotent + byte-identical
# ===========================================================================
def test_same_repo_rerun_is_idempotent_byte_identical(tmp_path: Path) -> None:
    ir = _pipeline()
    generate(ir, tmp_path, registry=_registry())
    first = _byte_snapshot(tmp_path)

    # two more runs — nothing should change
    generate(_pipeline(), tmp_path, registry=_registry())
    generate(_pipeline(), tmp_path, registry=_registry())
    third = _byte_snapshot(tmp_path)

    assert first.keys() == third.keys()
    for rel in first:
        assert first[rel] == third[rel], f"{rel} changed across identical re-runs"


# ===========================================================================
# 3. Manifest + bytes independent of plugin emission ORDER (sort stability)
# ===========================================================================
def test_emission_order_does_not_affect_output(tmp_path: Path) -> None:
    repo_sorted = tmp_path / "sorted"
    repo_reversed = tmp_path / "reversed"
    repo_sorted.mkdir()
    repo_reversed.mkdir()

    generate(_pipeline(), repo_sorted, registry=_registry(ci=_FakeCI()))
    generate(_pipeline(), repo_reversed, registry=_registry(ci=_ReversedEmitCI()))

    snap_sorted = _byte_snapshot(repo_sorted)
    snap_reversed = _byte_snapshot(repo_reversed)

    assert snap_sorted.keys() == snap_reversed.keys()
    for rel in snap_sorted:
        assert snap_sorted[rel] == snap_reversed[rel], f"{rel} differs by emission order"

    # explicitly: the manifest JSON is identical regardless of emission order
    assert snap_sorted[MANIFEST_RELPATH] == snap_reversed[MANIFEST_RELPATH]


# ===========================================================================
# 4. plan() is deterministic — equal tuples, manifest JSON, content hashes
# ===========================================================================
def test_plan_is_deterministic(tmp_path: Path) -> None:
    p1 = plan(_pipeline(), tmp_path, registry=_registry())
    p2 = plan(_pipeline(), tmp_path, registry=_registry())

    # equal file tuples (path + content + exec bit) in the same order
    assert p1.files == p2.files
    assert p1.paths == p2.paths

    # equal manifest serialization
    assert p1.manifest.to_json_str() == p2.manifest.to_json_str()

    # equal per-file content hashes
    h1 = {f.path: content_hash(f.content) for f in p1.files}
    h2 = {f.path: content_hash(f.content) for f in p2.files}
    assert h1 == h2

    # plan is pure: nothing written to disk
    assert not (tmp_path / MANIFEST_RELPATH).exists()


# ===========================================================================
# 5a. Many files whose hash-order differs from sorted order still sort stably
# ===========================================================================
def test_hash_ordering_does_not_leak_into_output(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    generate(_pipeline(), repo_a, registry=_registry(ci=_ManyFilesCI()))
    generate(_pipeline(), repo_b, registry=_registry(ci=_ManyFilesCI()))

    # manifest paths are strictly sorted (no dict/set iteration order leaking)
    plan_out = plan(_pipeline(), repo_a, registry=_registry(ci=_ManyFilesCI()))
    paths = list(plan_out.manifest.paths)
    assert paths == sorted(paths), "manifest paths must be lexicographically sorted"

    # and the two fresh repos are byte-identical despite the shuffled input
    assert _run_digest(repo_a) == _run_digest(repo_b)


# ===========================================================================
# 5b. No wall-clock timestamp sneaks into any generated file or the manifest
# ===========================================================================
def test_no_timestamp_leaks_and_sleep_is_irrelevant(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    generate(_pipeline(), repo_a, registry=_registry())
    # a real wall-clock gap between runs — if any timestamp leaked, bytes differ
    time.sleep(1.1)
    generate(_pipeline(), repo_b, registry=_registry())

    assert _run_digest(repo_a) == _run_digest(repo_b)

    # belt-and-braces: no current-year / epoch-looking token in any output file.
    now = datetime.now(UTC)
    year_tokens = {str(now.year), str(now.year - 1), str(now.year + 1)}
    # a plausible unix-epoch (10-digit) number would also indicate a clock leak.
    epoch_re = re.compile(r"\b1[0-9]{9}\b")

    for rel, data in _byte_snapshot(repo_a).items():
        text = data.decode("utf-8")
        for token in year_tokens:
            assert token not in text, f"year-like token {token!r} leaked into {rel}"
        assert not epoch_re.search(text), f"epoch-like timestamp leaked into {rel}"


# ===========================================================================
# 6. Single SHA-256 over all (path, bytes) pairs is stable across runs
# ===========================================================================
def test_run_digest_is_stable_across_runs(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_same = tmp_path / "same"
    for d in (repo_a, repo_b, repo_same):
        d.mkdir()

    generate(_pipeline(), repo_a, registry=_registry())
    generate(_pipeline(), repo_b, registry=_registry())

    # same IR + different fresh repos -> identical digest
    assert _run_digest(repo_a) == _run_digest(repo_b)

    # same repo, re-run twice -> digest unchanged
    generate(_pipeline(), repo_same, registry=_registry())
    digest_once = _run_digest(repo_same)
    generate(_pipeline(), repo_same, registry=_registry())
    assert _run_digest(repo_same) == digest_once


# ===========================================================================
# Sanity: the fixture plugins actually produce a non-trivial result
# ===========================================================================
def test_fixtures_produce_expected_files(tmp_path: Path) -> None:
    result = generate(_pipeline(), tmp_path, registry=_registry())
    assert isinstance(result, GenerationResult)
    # more than just the manifest got written
    assert len(result.written) > 1
    assert MANIFEST_RELPATH in result.written
