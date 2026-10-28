"""Unit tests for the plugin interface (task 3.1).

Covers the two stable provider contracts (R3.4, R4.1, R4.3, design §2/§8):

    * both base classes are abstract — a plugin missing a method cannot be
      instantiated (so a half-finished plugin fails loudly, not silently);
    * a complete CIVendorPlugin emits pipeline files for a stage graph;
    * a complete TargetPlatformPlugin implements all six verbs + supplies IaC;
    * ``run_verb`` dispatches each StageVerb to the matching method uniformly;
    * ``StageGraph.from_pipeline`` preserves declared stage order;
    * family / name identity lines up with the on-disk discovery convention.

These tests use tiny in-test plugins (no mocks) so they validate the real
contract, not a stand-in.
"""

from __future__ import annotations

import pytest

from cicdgen.ir import Pipeline
from cicdgen.plugins import (
    CIVendorPlugin,
    GeneratedFile,
    IaCArtifacts,
    PluginFamily,
    StageGraph,
    StageVerb,
    TargetPlatformPlugin,
    VerbContext,
    VerbResult,
)


# ---------------------------------------------------------------------------
# A minimal valid IR (two stages, declared order matters)
# ---------------------------------------------------------------------------
def _pipeline() -> Pipeline:
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [
                {"id": "test", "runs": "make test"},
                {"id": "build", "runs": "make build"},
            ],
        }
    )


# ---------------------------------------------------------------------------
# Tiny complete plugins used across the tests
# ---------------------------------------------------------------------------
class _FakeCI(CIVendorPlugin):
    name = "fake-ci"

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        stage_ids = ",".join(s.id for s in graph.stages)
        return (GeneratedFile(path=".ci/pipeline.yml", content=f"stages: {stage_ids}"),)


class _FakeTarget(TargetPlatformPlugin):
    name = "fake-target"

    def _echo(self, ctx: VerbContext) -> VerbResult:
        return VerbResult(commands=(f"{ctx.verb.value} {ctx.stage.id}",))

    lint = _echo
    test = _echo
    build = _echo
    package = _echo
    deploy = _echo
    verify = _echo

    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        return IaCArtifacts(
            files=(GeneratedFile(path="iac/main.tf", content="# tf"),),
            summary="demo infra",
        )


# ---------------------------------------------------------------------------
# Abstractness — half-finished plugins must fail at instantiation
# ---------------------------------------------------------------------------
def test_ci_vendor_plugin_is_abstract() -> None:
    with pytest.raises(TypeError):
        CIVendorPlugin()  # type: ignore[abstract]


def test_target_platform_plugin_is_abstract() -> None:
    with pytest.raises(TypeError):
        TargetPlatformPlugin()  # type: ignore[abstract]


def test_target_missing_one_verb_cannot_instantiate() -> None:
    class _Partial(TargetPlatformPlugin):
        name = "partial"

        def lint(self, ctx: VerbContext) -> VerbResult:  # noqa: D401
            return VerbResult()

        # test/build/package/deploy/verify + supply_iac intentionally missing

    with pytest.raises(TypeError):
        _Partial()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# CI-vendor family — emit pipeline files for a stage graph
# ---------------------------------------------------------------------------
def test_ci_vendor_emits_files_for_stage_graph() -> None:
    graph = StageGraph.from_pipeline(_pipeline())
    files = _FakeCI().emit_pipeline_files(graph)

    assert len(files) == 1
    assert files[0].path == ".ci/pipeline.yml"
    # declared order preserved: test before build
    assert files[0].content == "stages: test,build"


def test_stage_graph_preserves_declared_order() -> None:
    graph = StageGraph.from_pipeline(_pipeline())
    assert [s.id for s in graph.stages] == ["test", "build"]


# ---------------------------------------------------------------------------
# Target-platform family — verbs + IaC + uniform dispatch
# ---------------------------------------------------------------------------
def test_target_supplies_iac() -> None:
    iac = _FakeTarget().supply_iac(_pipeline())
    assert iac.summary == "demo infra"
    assert iac.files[0].path == "iac/main.tf"


@pytest.mark.parametrize("verb", list(StageVerb))
def test_run_verb_dispatches_every_verb(verb: StageVerb) -> None:
    pipeline = _pipeline()
    stage = pipeline.stages[0]
    ctx = VerbContext(pipeline=pipeline, stage=stage, verb=verb)

    result = _FakeTarget().run_verb(ctx)

    assert result.commands == (f"{verb.value} {stage.id}",)


def test_all_six_verbs_are_defined() -> None:
    assert {v.value for v in StageVerb} == {
        "lint",
        "test",
        "build",
        "package",
        "deploy",
        "verify",
    }


# ---------------------------------------------------------------------------
# Identity / discovery convention
# ---------------------------------------------------------------------------
def test_family_identity() -> None:
    assert _FakeCI().family is PluginFamily.CI_VENDOR
    assert _FakeTarget().family is PluginFamily.TARGET_PLATFORM


def test_family_values_match_on_disk_folders() -> None:
    # plugins/ci/ and plugins/target/ — the discovery roots (R3.4).
    assert PluginFamily.CI_VENDOR.value == "ci"
    assert PluginFamily.TARGET_PLATFORM.value == "target"
