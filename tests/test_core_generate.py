"""Unit tests for the generator core — validate → render → write + manifest (task 5.1).

Everything runs through an *injected* in-memory plugin registry so the core is
exercised before any concrete plugin exists (tasks 6-9) and without touching the
filesystem's plugin tree. Fake plugins are real subclasses (no mocks).

Proves:
    * files are written under the target repo root, with content + exec bit;
    * ``.cicd/generated.manifest.json`` lists every owned file with a content hash;
    * dry-run / ``plan`` writes nothing but reports the same file set;
    * re-running is byte-identical (determinism, R5.3) and idempotent;
    * an axis swap removes the stale files the previous manifest owned (R8.3);
    * the secret-value leak guard refuses obvious literal-credential leaks (R6.6),
      while by-reference forms pass;
    * a path escaping the repo root is refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cicdgen.core import (
    MANIFEST_RELPATH,
    GenerationError,
    GenerationPlan,
    GenerationResult,
    Manifest,
    content_hash,
    generate,
    load_manifest,
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

#: Capability sets for the in-memory fakes. A stand-in plugin declares the full
#: vocabulary so the §11.2 capability gate (new in task 17) is satisfied for any
#: IR these tests throw at it; the gate itself is exercised in its own test module.
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
# Fake plugins (in-memory registry)
# ---------------------------------------------------------------------------
class _FakeCI(CIVendorPlugin):
    name = "github"
    _content = "stages: ci"

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        stage_ids = ",".join(s.id for s in graph.stages)
        return (
            GeneratedFile(
                path=".github/workflows/ci.yml",
                content=f"# {self._content}\nstages: {stage_ids}\n",
            ),
            GeneratedFile(path="scripts/run.sh", content="#!/bin/sh\necho hi\n", executable=True),
        )


class _FakeTarget(TargetPlatformPlugin):
    name = "aws-ecs"
    capabilities = _ALL_CAPS

    def _verb(self, ctx: VerbContext) -> VerbResult:
        return VerbResult(
            commands=(f"{ctx.verb.value}",),
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


def _registry(ci: CIVendorPlugin | None = None, target: TargetPlatformPlugin | None = None) -> dict:
    return {
        (PluginFamily.CI_VENDOR, "github"): ci or _FakeCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): target or _FakeTarget(),
    }


# ===========================================================================
# Writing + manifest
# ===========================================================================
def test_generate_writes_files_under_repo_root(tmp_path: Path) -> None:
    result = generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry())

    assert isinstance(result, GenerationResult)
    # every written file is under the repo root and exists on disk
    for rel in result.written:
        full = tmp_path / rel
        assert full.is_file(), f"expected {rel} to be written"
        assert tmp_path.resolve() in full.resolve().parents

    ci = (tmp_path / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "stages: build" in ci
    # build stage -> build + package verb support files
    assert (tmp_path / "verb/build.sh").is_file()
    assert (tmp_path / "verb/package.sh").is_file()
    assert (tmp_path / "iac/main.tf").is_file()


def test_manifest_lists_every_owned_file_with_hash(tmp_path: Path) -> None:
    generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry())

    manifest_file = tmp_path / MANIFEST_RELPATH
    assert manifest_file.is_file()

    manifest = Manifest.from_json_str(manifest_file.read_text(encoding="utf-8"))
    assert manifest.ci_vendor == "github"
    assert manifest.target_platform == "aws-ecs"

    # every generated file (not the manifest itself) is tracked
    expected = {
        ".github/workflows/ci.yml",
        "scripts/run.sh",
        "verb/build.sh",
        "verb/package.sh",
        "iac/main.tf",
    }
    assert set(manifest.paths) == expected
    assert MANIFEST_RELPATH not in manifest.paths  # the manifest never lists itself

    # hashes match the on-disk content
    for entry in manifest.entries:
        disk = (tmp_path / entry.path).read_text(encoding="utf-8")
        assert entry.hash == content_hash(disk)


def test_manifest_records_executable_bit(tmp_path: Path) -> None:
    generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry())
    manifest = load_manifest(tmp_path)
    assert manifest is not None
    exec_entries = {e.path: e.executable for e in manifest.entries}
    assert exec_entries["scripts/run.sh"] is True
    assert exec_entries[".github/workflows/ci.yml"] is False


# ===========================================================================
# Dry-run / plan writes nothing
# ===========================================================================
def test_plan_writes_nothing(tmp_path: Path) -> None:
    the_plan = plan(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry())

    assert isinstance(the_plan, GenerationPlan)
    assert the_plan.files, "plan should list files that WOULD be written"
    # nothing on disk
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path / MANIFEST_RELPATH).exists()


def test_generate_dry_run_returns_plan_and_writes_nothing(tmp_path: Path) -> None:
    out = generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry(), dry_run=True)
    assert isinstance(out, GenerationPlan)
    assert list(tmp_path.iterdir()) == []


def test_plan_paths_match_real_run(tmp_path: Path) -> None:
    the_plan = plan(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry())
    planned = set(the_plan.paths)

    result = generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry())
    written = set(result.written) - {MANIFEST_RELPATH}
    assert planned == written


# ===========================================================================
# Determinism + idempotent re-run (R5.3)
# ===========================================================================
def test_rerun_is_byte_identical(tmp_path: Path) -> None:
    generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry())
    snapshot = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}

    # run again into the same repo
    generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry())
    after = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}

    assert snapshot.keys() == after.keys()
    for p in snapshot:
        assert snapshot[p] == after[p], f"{p} changed between identical runs"


def test_manifest_serialization_is_deterministic() -> None:
    files = [
        GeneratedFile(path="b.txt", content="b"),
        GeneratedFile(path="a.txt", content="a"),
    ]
    m1 = Manifest.build(ci_vendor="github", target_platform="aws-ecs", files=files)
    m2 = Manifest.build(ci_vendor="github", target_platform="aws-ecs", files=list(reversed(files)))
    # input order does not matter; output is sorted + identical
    assert m1.to_json_str() == m2.to_json_str()
    assert m1.paths == ("a.txt", "b.txt")
    assert m1.to_json_str().endswith("\n")


def test_two_plans_identical_inputs_equal(tmp_path: Path) -> None:
    p1 = plan(_pipeline(stage_ids=("test", "build")), tmp_path, registry=_registry())
    p2 = plan(_pipeline(stage_ids=("test", "build")), tmp_path, registry=_registry())
    assert p1.paths == p2.paths
    assert p1.manifest.to_json_str() == p2.manifest.to_json_str()


# ===========================================================================
# Axis swap — stale files removed (R8.3)
# ===========================================================================
def test_axis_swap_removes_stale_files(tmp_path: Path) -> None:
    # First run: CI plugin emits ci.yml + run.sh
    generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry())
    assert (tmp_path / ".github/workflows/ci.yml").is_file()
    assert (tmp_path / "scripts/run.sh").is_file()

    # Swap the CI plugin for one that emits a *different* file set.
    class _OtherCI(CIVendorPlugin):
        name = "github"

        def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
            return (GeneratedFile(path="azure-pipelines.yml", content="pool: default\n"),)

    swapped = _registry(ci=_OtherCI())
    result = generate(_pipeline(stage_ids=("build",)), tmp_path, registry=swapped)

    # new file written, old CI files removed, verb/iac files still present
    assert (tmp_path / "azure-pipelines.yml").is_file()
    assert not (tmp_path / ".github/workflows/ci.yml").exists()
    assert not (tmp_path / "scripts/run.sh").exists()
    assert ".github/workflows/ci.yml" in result.removed
    assert "scripts/run.sh" in result.removed

    manifest = load_manifest(tmp_path)
    assert manifest is not None
    assert "azure-pipelines.yml" in manifest.paths
    assert ".github/workflows/ci.yml" not in manifest.paths


def test_plan_reports_stale_files_without_removing(tmp_path: Path) -> None:
    generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry())

    class _OtherCI(CIVendorPlugin):
        name = "github"

        def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
            return (GeneratedFile(path="azure-pipelines.yml", content="x\n"),)

    the_plan = plan(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry(ci=_OtherCI()))
    assert ".github/workflows/ci.yml" in the_plan.removed
    # still on disk — plan removes nothing
    assert (tmp_path / ".github/workflows/ci.yml").is_file()


# ===========================================================================
# Secret-value leak guard (R6.6)
# ===========================================================================
def test_secret_value_leak_is_refused(tmp_path: Path) -> None:
    class _LeakyCI(CIVendorPlugin):
        name = "github"

        def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
            return (
                GeneratedFile(
                    path=".github/workflows/ci.yml",
                    content='env:\n  password: "hunter2-super-secret"\n',
                ),
            )

    with pytest.raises(GenerationError, match=r"secret value"):
        generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry(ci=_LeakyCI()))
    # nothing written on refusal
    assert not (tmp_path / ".github/workflows/ci.yml").exists()


def test_secret_reference_form_passes(tmp_path: Path) -> None:
    class _RefCI(CIVendorPlugin):
        name = "github"

        def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
            return (
                GeneratedFile(
                    path=".github/workflows/ci.yml",
                    content="env:\n  TOKEN: ${{ secrets.CICD_TOKEN }}\n",
                ),
            )

    # by-reference form must NOT trip the guard
    result = generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry(ci=_RefCI()))
    assert isinstance(result, GenerationResult)
    assert (tmp_path / ".github/workflows/ci.yml").is_file()


def test_scan_secrets_can_be_disabled(tmp_path: Path) -> None:
    class _LeakyCI(CIVendorPlugin):
        name = "github"

        def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
            return (
                GeneratedFile(
                    path=".github/workflows/ci.yml",
                    content='password = "hunter2-super-secret"\n',
                ),
            )

    # explicit opt-out writes the file
    result = generate(
        _pipeline(stage_ids=("build",)),
        tmp_path,
        registry=_registry(ci=_LeakyCI()),
        scan_secrets=False,
    )
    assert isinstance(result, GenerationResult)
    assert (tmp_path / ".github/workflows/ci.yml").is_file()


# ===========================================================================
# Path-escape guard
# ===========================================================================
def test_path_escape_is_refused(tmp_path: Path) -> None:
    class _EscapingCI(CIVendorPlugin):
        name = "github"

        def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
            return (GeneratedFile(path="../escape.yml", content="nope\n"),)

    with pytest.raises(GenerationError, match=r"outside the repo root"):
        generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry(ci=_EscapingCI()))


# ===========================================================================
# Accepts a pre-validated Pipeline as well as a path
# ===========================================================================
def test_generate_accepts_pipeline_object(tmp_path: Path) -> None:
    result = generate(_pipeline(stage_ids=("build",)), tmp_path, registry=_registry())
    assert isinstance(result, GenerationResult)
    assert (tmp_path / MANIFEST_RELPATH).is_file()


def test_generate_accepts_ir_path(tmp_path: Path) -> None:
    ir_file = tmp_path / "pipeline.yaml"
    ir_file.write_text(
        "version: 1\n"
        "ci_vendor: github\n"
        "target_platform: aws-ecs\n"
        "project:\n  name: demo\n"
        "environments:\n  - name: dev\n"
        "stages:\n  - id: build\n",
        encoding="utf-8",
    )
    out_repo = tmp_path / "repo"
    out_repo.mkdir()
    # registry injected -> on-disk plugin existence check is skipped automatically
    result = generate(ir_file, out_repo, registry=_registry())
    assert isinstance(result, GenerationResult)
    assert (out_repo / ".github/workflows/ci.yml").is_file()
