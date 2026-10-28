"""Manifest-driven axis-swap cleanup + detection — task 11.1 (R8.3, R3.5).

The generator core records every file it owns in ``.cicd/generated.manifest.json``
and, on a later run, removes the paths the *previous* manifest owned that the new
run no longer owns (``cicdgen.core.generate``). Task 11.1 formalizes that as a
first-class *axis-swap migration*: when a run changes ``ci_vendor`` and/or
``target_platform``, the OLD vendor/target files the generator owned are removed,
the NEW ones written, the manifest records the new axes, and the run is *flagged*
as an axis change (``GenerationResult.axis_change`` / ``detect_axis_change``).

Everything runs through an *injected* in-memory registry of real plugin
subclasses (no mocks), with two CI plugins and two target plugins so a swap on
either axis can be exercised end to end:

    CI:     github       -> .github/workflows/ci.yml + scripts/run.sh
            azure-devops -> azure-pipelines.yml
    target: aws-ecs      -> Dockerfile + iac/aws-ecs/main.tf
            databricks   -> databricks/job.json + iac/databricks/main.tf (NO Dockerfile)

Proves:
    * CI swap (github -> azure-devops): old CI files removed, new CI file written,
      target (aws-ecs) files untouched, client file untouched, manifest records
      the new ci_vendor;
    * target swap (aws-ecs -> databricks): old target files removed (incl. the
      Dockerfile), new target files written, CI file untouched, manifest records
      the new target_platform;
    * the run is flagged as an axis change; a same-axes regenerate is NOT flagged;
    * a first run is not an axis change (axis_change is None);
    * client-authored files (never in the manifest) are NEVER removed — the
      manifest-ownership safety guarantee.
"""

from __future__ import annotations

from pathlib import Path

from cicdgen.core import (
    AxisChange,
    GenerationResult,
    detect_axis_change_for_repo,
    generate,
    load_manifest,
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

CLIENT_FILE = "src/app.py"
CLIENT_CONTENT = "print('client authored — generator must never touch me')\n"


# ---------------------------------------------------------------------------
# IR helper
# ---------------------------------------------------------------------------
def _pipeline(*, ci_vendor: str, target_platform: str) -> Pipeline:
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": ci_vendor,
            "target_platform": target_platform,
            "project": {"name": "demo"},
            "environments": [{"name": "dev"}],
            "stages": [{"id": "build"}],
        }
    )


# ---------------------------------------------------------------------------
# Two CI-vendor fakes (different owned file sets)
# ---------------------------------------------------------------------------
class _GitHubCI(CIVendorPlugin):
    name = "github"

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        stages = ",".join(s.id for s in graph.stages)
        return (
            GeneratedFile(
                path=".github/workflows/ci.yml",
                content=f"# github actions\non: push\nstages: {stages}\n",
            ),
            GeneratedFile(
                path="scripts/run.sh",
                content="#!/bin/sh\necho github\n",
                executable=True,
            ),
        )


class _AzureDevOpsCI(CIVendorPlugin):
    name = "azure-devops"

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        stages = ",".join(s.id for s in graph.stages)
        return (
            GeneratedFile(
                path="azure-pipelines.yml",
                content=f"# azure devops\npool: default\nstages: {stages}\n",
            ),
        )


# ---------------------------------------------------------------------------
# Two target-platform fakes (different owned file sets; databricks has NO image)
# ---------------------------------------------------------------------------
class _AwsEcsTarget(TargetPlatformPlugin):
    name = "aws-ecs"
    capabilities = _ALL_CAPS

    def _verb(self, ctx: VerbContext) -> VerbResult:
        # build emits a Dockerfile (image-shaped platform); other verbs no-op files.
        if ctx.verb.value == "build":
            return VerbResult(
                commands=("docker build -t demo .",),
                files=(GeneratedFile(path="Dockerfile", content="FROM scratch\n"),),
            )
        return VerbResult(commands=(ctx.verb.value,))

    lint = _verb
    test = _verb
    build = _verb
    package = _verb
    deploy = _verb
    verify = _verb

    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        return IaCArtifacts(
            files=(
                GeneratedFile(
                    path="iac/aws-ecs/main.tf",
                    content="# ECR + ECS + IAM OIDC\n",
                ),
            ),
            summary="aws-ecs infra",
        )


class _DatabricksTarget(TargetPlatformPlugin):
    name = "databricks"
    capabilities = _ALL_CAPS

    def _verb(self, ctx: VerbContext) -> VerbResult:
        # NO image: build assembles a job spec instead of a Dockerfile.
        if ctx.verb.value == "build":
            return VerbResult(
                commands=("assemble wheel + notebooks",),
                files=(
                    GeneratedFile(
                        path="databricks/job.json",
                        content='{"name": "demo-job"}\n',
                    ),
                ),
            )
        return VerbResult(commands=(ctx.verb.value,))

    lint = _verb
    test = _verb
    build = _verb
    package = _verb
    deploy = _verb
    verify = _verb

    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        return IaCArtifacts(
            files=(
                GeneratedFile(
                    path="iac/databricks/main.tf",
                    content="# workspace + job + UC\n",
                ),
            ),
            summary="databricks infra",
        )


def _registry() -> dict:
    """A registry carrying BOTH CI plugins and BOTH target plugins."""
    return {
        (PluginFamily.CI_VENDOR, "github"): _GitHubCI(),
        (PluginFamily.CI_VENDOR, "azure-devops"): _AzureDevOpsCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): _AwsEcsTarget(),
        (PluginFamily.TARGET_PLATFORM, "databricks"): _DatabricksTarget(),
    }


def _write_client_file(repo: Path) -> None:
    """Author a file the generator never emits (not in any manifest)."""
    target = repo / CLIENT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(CLIENT_CONTENT, encoding="utf-8")


def _assert_client_file_untouched(repo: Path) -> None:
    client = repo / CLIENT_FILE
    assert client.is_file(), "client-authored file must never be removed"
    assert client.read_text(encoding="utf-8") == CLIENT_CONTENT


# ===========================================================================
# First run is not an axis change
# ===========================================================================
def test_first_run_is_not_an_axis_change(tmp_path: Path) -> None:
    result = generate(
        _pipeline(ci_vendor="github", target_platform="aws-ecs"),
        tmp_path,
        registry=_registry(),
    )
    assert isinstance(result, GenerationResult)
    # no prior manifest -> nothing to migrate from
    assert result.axis_change is None


# ===========================================================================
# CI swap: github -> azure-devops (same target aws-ecs)
# ===========================================================================
def test_ci_swap_removes_old_ci_files_keeps_target_and_client(tmp_path: Path) -> None:
    reg = _registry()

    # First run: github x aws-ecs
    generate(_pipeline(ci_vendor="github", target_platform="aws-ecs"), tmp_path, registry=reg)
    assert (tmp_path / ".github/workflows/ci.yml").is_file()
    assert (tmp_path / "scripts/run.sh").is_file()
    assert (tmp_path / "Dockerfile").is_file()
    assert (tmp_path / "iac/aws-ecs/main.tf").is_file()

    # Client authors a file the generator does not own.
    _write_client_file(tmp_path)

    # Regenerate with the CI vendor swapped.
    result = generate(
        _pipeline(ci_vendor="azure-devops", target_platform="aws-ecs"),
        tmp_path,
        registry=reg,
    )
    assert isinstance(result, GenerationResult)

    # new CI file written
    assert (tmp_path / "azure-pipelines.yml").is_file()
    # old CI files removed
    assert not (tmp_path / ".github/workflows/ci.yml").exists()
    assert not (tmp_path / "scripts/run.sh").exists()
    assert ".github/workflows/ci.yml" in result.removed
    assert "scripts/run.sh" in result.removed
    # aws-ecs target files remain (target did not change)
    assert (tmp_path / "Dockerfile").is_file()
    assert (tmp_path / "iac/aws-ecs/main.tf").is_file()
    assert "Dockerfile" not in result.removed
    assert "iac/aws-ecs/main.tf" not in result.removed

    # client file untouched
    _assert_client_file_untouched(tmp_path)

    # manifest now records azure-devops, target unchanged
    manifest = load_manifest(tmp_path)
    assert manifest is not None
    assert manifest.ci_vendor == "azure-devops"
    assert manifest.target_platform == "aws-ecs"
    assert "azure-pipelines.yml" in manifest.paths
    assert ".github/workflows/ci.yml" not in manifest.paths

    # flagged as an axis change — CI only
    assert result.axis_change is not None
    assert result.axis_change.is_axis_change is True
    assert result.axis_change.ci_changed is True
    assert result.axis_change.target_changed is False
    assert result.axis_change.old_ci_vendor == "github"
    assert result.axis_change.new_ci_vendor == "azure-devops"


# ===========================================================================
# Target swap: aws-ecs -> databricks (same CI azure-devops)
# ===========================================================================
def test_target_swap_removes_old_target_files_keeps_ci_and_client(tmp_path: Path) -> None:
    reg = _registry()

    # Arrive at azure-devops x aws-ecs (first run then CI swap).
    generate(_pipeline(ci_vendor="github", target_platform="aws-ecs"), tmp_path, registry=reg)
    _write_client_file(tmp_path)
    generate(_pipeline(ci_vendor="azure-devops", target_platform="aws-ecs"), tmp_path, registry=reg)
    assert (tmp_path / "Dockerfile").is_file()
    assert (tmp_path / "iac/aws-ecs/main.tf").is_file()
    assert (tmp_path / "azure-pipelines.yml").is_file()

    # Regenerate with the target platform swapped.
    result = generate(
        _pipeline(ci_vendor="azure-devops", target_platform="databricks"),
        tmp_path,
        registry=reg,
    )
    assert isinstance(result, GenerationResult)

    # new target files written
    assert (tmp_path / "databricks/job.json").is_file()
    assert (tmp_path / "iac/databricks/main.tf").is_file()
    # old aws-ecs target files removed — including the Dockerfile (databricks has no image)
    assert not (tmp_path / "Dockerfile").exists()
    assert not (tmp_path / "iac/aws-ecs/main.tf").exists()
    assert "Dockerfile" in result.removed
    assert "iac/aws-ecs/main.tf" in result.removed
    # CI file remains (CI vendor did not change)
    assert (tmp_path / "azure-pipelines.yml").is_file()
    assert "azure-pipelines.yml" not in result.removed

    # client file untouched
    _assert_client_file_untouched(tmp_path)

    # manifest now records databricks, CI unchanged
    manifest = load_manifest(tmp_path)
    assert manifest is not None
    assert manifest.ci_vendor == "azure-devops"
    assert manifest.target_platform == "databricks"
    assert "databricks/job.json" in manifest.paths
    assert "Dockerfile" not in manifest.paths

    # flagged as an axis change — target only
    assert result.axis_change is not None
    assert result.axis_change.is_axis_change is True
    assert result.axis_change.target_changed is True
    assert result.axis_change.ci_changed is False
    assert result.axis_change.old_target_platform == "aws-ecs"
    assert result.axis_change.new_target_platform == "databricks"


# ===========================================================================
# Both axes at once
# ===========================================================================
def test_both_axes_swap_in_one_run(tmp_path: Path) -> None:
    reg = _registry()
    generate(_pipeline(ci_vendor="github", target_platform="aws-ecs"), tmp_path, registry=reg)

    result = generate(
        _pipeline(ci_vendor="azure-devops", target_platform="databricks"),
        tmp_path,
        registry=reg,
    )
    assert isinstance(result, GenerationResult)
    # both old CI and old target files removed
    assert {".github/workflows/ci.yml", "scripts/run.sh", "Dockerfile", "iac/aws-ecs/main.tf"} <= set(
        result.removed
    )
    assert (tmp_path / "azure-pipelines.yml").is_file()
    assert (tmp_path / "databricks/job.json").is_file()

    assert result.axis_change is not None
    assert result.axis_change.ci_changed is True
    assert result.axis_change.target_changed is True
    assert result.axis_change.is_axis_change is True


# ===========================================================================
# A same-axes regenerate is NOT flagged as an axis change
# ===========================================================================
def test_no_change_regenerate_is_not_flagged(tmp_path: Path) -> None:
    reg = _registry()
    generate(_pipeline(ci_vendor="github", target_platform="aws-ecs"), tmp_path, registry=reg)

    result = generate(
        _pipeline(ci_vendor="github", target_platform="aws-ecs"),
        tmp_path,
        registry=reg,
    )
    assert isinstance(result, GenerationResult)
    # a prior manifest exists, so axis_change is an AxisChange — but nothing changed
    assert result.axis_change is not None
    assert result.axis_change.is_axis_change is False
    assert result.axis_change.ci_changed is False
    assert result.axis_change.target_changed is False
    # nothing removed on an idempotent re-run
    assert result.removed == ()


# ===========================================================================
# Safety: client-authored files are never removed across repeated swaps
# ===========================================================================
def test_client_files_never_removed_across_swaps(tmp_path: Path) -> None:
    reg = _registry()
    generate(_pipeline(ci_vendor="github", target_platform="aws-ecs"), tmp_path, registry=reg)
    _write_client_file(tmp_path)

    # A whole chain of swaps — the client file must survive every one.
    generate(_pipeline(ci_vendor="azure-devops", target_platform="aws-ecs"), tmp_path, registry=reg)
    _assert_client_file_untouched(tmp_path)
    generate(_pipeline(ci_vendor="azure-devops", target_platform="databricks"), tmp_path, registry=reg)
    _assert_client_file_untouched(tmp_path)
    generate(_pipeline(ci_vendor="github", target_platform="databricks"), tmp_path, registry=reg)
    _assert_client_file_untouched(tmp_path)

    # the client file is never recorded as owned by the generator
    manifest = load_manifest(tmp_path)
    assert manifest is not None
    assert CLIENT_FILE not in manifest.paths


# ===========================================================================
# detect_axis_change_for_repo: standalone detection helper
# ===========================================================================
def test_detect_axis_change_for_repo_before_and_after(tmp_path: Path) -> None:
    reg = _registry()

    # no manifest yet -> None
    assert (
        detect_axis_change_for_repo(
            tmp_path, new_ci_vendor="github", new_target_platform="aws-ecs"
        )
        is None
    )

    generate(_pipeline(ci_vendor="github", target_platform="aws-ecs"), tmp_path, registry=reg)

    # same axes -> AxisChange, not flagged
    same = detect_axis_change_for_repo(
        tmp_path, new_ci_vendor="github", new_target_platform="aws-ecs"
    )
    assert isinstance(same, AxisChange)
    assert same.is_axis_change is False

    # changed axes -> flagged, summary names only the axes (no secrets)
    changed = detect_axis_change_for_repo(
        tmp_path, new_ci_vendor="azure-devops", new_target_platform="databricks"
    )
    assert isinstance(changed, AxisChange)
    assert changed.is_axis_change is True
    summary = changed.summary()
    assert "github -> azure-devops" in summary
    assert "aws-ecs -> databricks" in summary
