"""Drift detection — `cicd doctor` round-trip + read-only property (task 22.4).

This module is the §11.7 drift engine under test. It drives the core
:func:`cicdgen.core.doctor.check_drift` through an *injected* in-memory plugin
registry (the same style as ``test_core_generate.py`` — real subclasses, no mocks)
so the drift round-trip is exercised without touching the on-disk plugin tree, and
asserts the read-only guarantee by snapshotting the whole repo tree before/after.

Following the project's parametrized-property convention (NO hypothesis dependency —
inputs are enumerated/seeded deterministically), mirroring
``test_promotion.py`` / ``test_snapshot_harness.py``.

Property 10 — drift detection round-trip and read-only
------------------------------------------------------
Feature: agentic-cicd-generator, Property 10: Drift detection round-trip and
read-only — for any freshly generated repo, `cicd doctor` reports zero drift
immediately after `generate`; for any single committed-file mutation, deletion, or
addition, `doctor` reports exactly the affected path(s) and the category
(changed / missing / extra), and the on-disk file set is unchanged after `doctor`
runs (it writes nothing).
Validates: Requirements R5.3, R9.2 (Plan B §11.7).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cicdgen.core import (
    MANIFEST_RELPATH,
    DoctorError,
    DriftCategory,
    check_drift,
    generate,
    render_drift_report,
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

_ALL_CAPS = frozenset(Capability)


# ---------------------------------------------------------------------------
# IR + fake plugins (in-memory registry) — identical shape to test_core_generate
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


class _FakeCI(CIVendorPlugin):
    name = "github"

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        stage_ids = ",".join(s.id for s in graph.stages)
        return (
            GeneratedFile(
                path=".github/workflows/ci.yml",
                content=f"# stages: {stage_ids}\n",
            ),
            GeneratedFile(
                path="scripts/run.sh", content="#!/bin/sh\necho hi\n", executable=True
            ),
        )


class _FakeTarget(TargetPlatformPlugin):
    name = "aws-ecs"
    capabilities = _ALL_CAPS

    def _verb(self, ctx: VerbContext) -> VerbResult:
        return VerbResult(
            commands=(ctx.verb.value,),
            files=(
                GeneratedFile(path=f"verb/{ctx.verb.value}.sh", content=ctx.verb.value + "\n"),
            ),
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


def _registry() -> dict:
    return {
        (PluginFamily.CI_VENDOR, "github"): _FakeCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): _FakeTarget(),
    }


def _snapshot_tree(root: Path) -> dict[Path, bytes]:
    """Byte snapshot of every file under ``root`` — used to prove nothing was written."""
    return {p: p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _generate(tmp_path: Path, pipeline: Pipeline) -> None:
    generate(pipeline, tmp_path, registry=_registry())


def _check(tmp_path: Path, pipeline: Pipeline):
    return check_drift(pipeline, tmp_path, registry=_registry(), check_plugins=False)


# ===========================================================================
# Round-trip: a freshly generated repo reports ZERO drift (R5.3)
# ===========================================================================
@pytest.mark.parametrize(
    "stage_ids",
    [
        ("build",),
        ("test", "build"),
        ("test", "build", "deploy"),
    ],
)
def test_property10_clean_generate_has_no_drift(
    tmp_path: Path, stage_ids: tuple[str, ...]
) -> None:
    """Immediately after a clean generate, doctor reports zero drift (round-trip)."""
    pipeline = _pipeline(stage_ids=stage_ids)
    _generate(tmp_path, pipeline)

    report = _check(tmp_path, pipeline)

    assert not report.has_drift, render_drift_report(report)
    assert report.missing == ()
    assert report.extra == ()
    assert report.changed == ()
    # Every owned path classified in-sync (and there is at least one).
    assert report.in_sync
    assert all(e.category is DriftCategory.IN_SYNC for e in report.entries)


# ===========================================================================
# A single mutation is detected in the right category
# ===========================================================================
def test_property10_mutating_an_owned_file_is_changed(tmp_path: Path) -> None:
    """Editing an owned file => exactly that path reported CHANGED, nothing else."""
    pipeline = _pipeline(stage_ids=("build",))
    _generate(tmp_path, pipeline)

    victim = tmp_path / ".github/workflows/ci.yml"
    victim.write_text("# tampered\n", encoding="utf-8", newline="")

    report = _check(tmp_path, pipeline)

    assert report.has_drift
    changed_paths = [e.path for e in report.changed]
    assert changed_paths == [".github/workflows/ci.yml"]
    assert report.missing == ()
    assert report.extra == ()


def test_property10_removing_an_owned_file_is_missing(tmp_path: Path) -> None:
    """Deleting an owned file => exactly that path reported MISSING, nothing else."""
    pipeline = _pipeline(stage_ids=("build",))
    _generate(tmp_path, pipeline)

    (tmp_path / "iac/main.tf").unlink()

    report = _check(tmp_path, pipeline)

    assert report.has_drift
    missing_paths = [e.path for e in report.missing]
    assert missing_paths == ["iac/main.tf"]
    assert report.changed == ()
    assert report.extra == ()


def test_property10_extra_owned_file_no_longer_produced_is_extra(tmp_path: Path) -> None:
    """A manifest-owned path the generator no longer produces => reported EXTRA.

    We simulate drift the other direction: generate with two stages, then re-check
    against a pipeline whose render drops a verb file. The manifest still owns the
    now-unproduced path, so it must surface as EXTRA (a real run would remove it).
    """
    pipeline = _pipeline(stage_ids=("test", "build"))
    _generate(tmp_path, pipeline)

    # A target plugin that drops the "test" verb file so its path is no longer
    # produced, while the manifest on disk still owns verb/test.sh.
    def _no_test_verb(self: _FakeTarget, ctx: VerbContext) -> VerbResult:
        if ctx.verb.value == "test":
            return VerbResult(commands=(), files=())
        return VerbResult(
            commands=(ctx.verb.value,),
            files=(
                GeneratedFile(path=f"verb/{ctx.verb.value}.sh", content=ctx.verb.value + "\n"),
            ),
        )

    class _NoTestTarget(_FakeTarget):
        lint = _no_test_verb
        test = _no_test_verb
        build = _no_test_verb
        package = _no_test_verb
        deploy = _no_test_verb
        verify = _no_test_verb

    registry = {
        (PluginFamily.CI_VENDOR, "github"): _FakeCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): _NoTestTarget(),
    }
    report = check_drift(pipeline, tmp_path, registry=registry, check_plugins=False)

    assert report.has_drift
    extra_paths = [e.path for e in report.extra]
    assert "verb/test.sh" in extra_paths
    # the test.sh path drifts as EXTRA; nothing is spuriously MISSING
    assert report.missing == ()


# ===========================================================================
# Read-only guarantee: doctor writes NOTHING to the repo (R9.2)
# ===========================================================================
@pytest.mark.parametrize(
    "mutate",
    [
        "none",
        "change",
        "remove",
    ],
)
def test_property10_doctor_writes_nothing(tmp_path: Path, mutate: str) -> None:
    """Running doctor leaves the on-disk file set byte-identical (writes nothing)."""
    pipeline = _pipeline(stage_ids=("test", "build"))
    _generate(tmp_path, pipeline)

    if mutate == "change":
        (tmp_path / ".github/workflows/ci.yml").write_text(
            "# tampered\n", encoding="utf-8", newline=""
        )
    elif mutate == "remove":
        (tmp_path / "verb/build.sh").unlink()

    before = _snapshot_tree(tmp_path)
    before_names = set(before)

    # Two runs to be extra sure doctor is idempotent and side-effect-free.
    _check(tmp_path, pipeline)
    _check(tmp_path, pipeline)

    after = _snapshot_tree(tmp_path)

    assert set(after) == before_names, "doctor added or removed a file"
    for p in before:
        assert after[p] == before[p], f"doctor modified {p}"


# ===========================================================================
# No committed manifest => actionable error, not a crash
# ===========================================================================
def test_check_drift_without_manifest_raises(tmp_path: Path) -> None:
    """Running doctor before any generate names the missing manifest (DoctorError)."""
    with pytest.raises(DoctorError, match=r"no committed manifest"):
        _check(tmp_path, _pipeline(stage_ids=("build",)))


def test_manifest_itself_is_not_part_of_the_drift_set(tmp_path: Path) -> None:
    """The generator-bookkeeping manifest is never one of the owned drift paths."""
    pipeline = _pipeline(stage_ids=("build",))
    _generate(tmp_path, pipeline)
    report = _check(tmp_path, pipeline)
    assert MANIFEST_RELPATH not in {e.path for e in report.entries}
