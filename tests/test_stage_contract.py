"""Unit tests for the stable stage-verb contract (task 3.2).

Proves the core invokes verbs *uniformly, independent of platform* (R4.1, R4.3)
with NO vendor/platform branching (R3.4):

    * the declarative stage -> verbs policy maps representative stage ids to the
      right ordered verbs (test-shaped, build-shaped, deploy-shaped, CI-only,
      unknown);
    * ``invoke_stage`` calls the plugin's uniform ``run_verb`` for each mapped verb
      in order and composes commands/files;
    * a disabled stage runs no verbs;
    * an explicit override pins verbs regardless of the stage id;
    * two differently-shaped fake platforms are driven by the SAME core code path,
      with the only difference being the ``VerbResult`` each returns — i.e. the
      core has zero platform-specific branching;
    * ``invoke_pipeline`` walks stages in declared order.

The fake plugins are real ``TargetPlatformPlugin`` subclasses (no mocks) that
*record* the order in which their verbs were called, so the tests assert on the
actual invocation sequence the core produced.
"""

from __future__ import annotations

import pytest

from cicdgen.ir import Pipeline, Stage
from cicdgen.plugins import (
    GeneratedFile,
    IaCArtifacts,
    StageInvocation,
    StageVerb,
    TargetPlatformPlugin,
    VerbContext,
    VerbResult,
    invoke_pipeline,
    invoke_stage,
    verbs_for_stage,
)


# ---------------------------------------------------------------------------
# IR helpers
# ---------------------------------------------------------------------------
def _stage(stage_id: str, *, enabled: bool = True) -> Stage:
    return Stage.model_validate({"id": stage_id, "enabled": enabled})


def _pipeline(stage_ids: list[str]) -> Pipeline:
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [{"id": sid} for sid in stage_ids],
        }
    )


# ---------------------------------------------------------------------------
# Recording fake plugins — real subclasses, no mocks.
# Two *different shapes* driven by the SAME core path (proves no branching).
# ---------------------------------------------------------------------------
class _RecordingTarget(TargetPlatformPlugin):
    """Records every verb call; each verb emits a tagged command + a file."""

    name = "recording-target"

    def __init__(self) -> None:
        self.calls: list[tuple[StageVerb, str]] = []

    def _run(self, ctx: VerbContext) -> VerbResult:
        self.calls.append((ctx.verb, ctx.stage.id))
        return VerbResult(
            commands=(f"{ctx.verb.value}:{ctx.stage.id}",),
            files=(GeneratedFile(path=f"{ctx.verb.value}.sh", content=ctx.verb.value),),
        )

    lint = _run
    test = _run
    build = _run
    package = _run
    deploy = _run
    verify = _run

    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        return IaCArtifacts()


class _ImagelessTarget(TargetPlatformPlugin):
    """A databricks-like shape: ``package`` is a no-op (no image), proving the

    uniform contract holds even when a platform does nothing for a verb (§8b).
    """

    name = "imageless-target"

    def __init__(self) -> None:
        self.calls: list[StageVerb] = []

    def _cmd(self, ctx: VerbContext) -> VerbResult:
        self.calls.append(ctx.verb)
        return VerbResult(commands=(f"databricks-{ctx.verb.value}",))

    def _noop(self, ctx: VerbContext) -> VerbResult:
        self.calls.append(ctx.verb)
        return VerbResult()  # no image to package

    lint = _cmd
    test = _cmd
    build = _cmd
    package = _noop
    deploy = _cmd
    verify = _cmd

    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        return IaCArtifacts()


# ---------------------------------------------------------------------------
# The declarative policy — representative stage ids
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("stage_id", "expected"),
    [
        ("test", (StageVerb.LINT, StageVerb.TEST)),
        ("unit-test", (StageVerb.LINT, StageVerb.TEST)),
        ("lint", (StageVerb.LINT, StageVerb.TEST)),
        ("build", (StageVerb.BUILD, StageVerb.PACKAGE)),
        ("compile", (StageVerb.BUILD, StageVerb.PACKAGE)),
        ("deploy-dev", (StageVerb.DEPLOY, StageVerb.VERIFY)),
        ("deploy-prod", (StageVerb.DEPLOY, StageVerb.VERIFY)),
        ("release", (StageVerb.DEPLOY, StageVerb.VERIFY)),
    ],
)
def test_policy_maps_stage_to_ordered_verbs(
    stage_id: str, expected: tuple[StageVerb, ...]
) -> None:
    assert verbs_for_stage(_stage(stage_id)) == expected


@pytest.mark.parametrize("stage_id", ["security", "secret-scan", "prod-approval", "notify"])
def test_ci_only_stages_have_no_target_verbs(stage_id: str) -> None:
    # security/scan/approval/notify are CI-vendor concerns (§8a), not target verbs.
    assert verbs_for_stage(_stage(stage_id)) == ()


def test_unknown_stage_id_contributes_no_verbs() -> None:
    # An id the policy doesn't recognise runs nothing rather than guessing wrong.
    assert verbs_for_stage(_stage("totally-custom")) == ()


def test_explicit_override_pins_verbs_regardless_of_id() -> None:
    # Even a "test"-looking id yields exactly the override, in order.
    override = [StageVerb.DEPLOY, StageVerb.VERIFY]
    assert verbs_for_stage(_stage("test"), explicit=override) == (
        StageVerb.DEPLOY,
        StageVerb.VERIFY,
    )


def test_policy_is_case_insensitive() -> None:
    assert verbs_for_stage(_stage("Deploy-PROD")) == (StageVerb.DEPLOY, StageVerb.VERIFY)


# ---------------------------------------------------------------------------
# invoke_stage — uniform invocation in the right order
# ---------------------------------------------------------------------------
def test_invoke_stage_calls_verbs_in_order() -> None:
    pipeline = _pipeline(["build"])
    plugin = _RecordingTarget()

    inv = invoke_stage(plugin, pipeline, pipeline.stages[0])

    # the plugin actually received build then package, for the "build" stage
    assert plugin.calls == [(StageVerb.BUILD, "build"), (StageVerb.PACKAGE, "build")]
    assert inv.verbs == (StageVerb.BUILD, StageVerb.PACKAGE)
    assert inv.commands == ("build:build", "package:build")


def test_invoke_stage_composes_files_deduped_by_path() -> None:
    pipeline = _pipeline(["deploy-dev"])
    plugin = _RecordingTarget()

    inv = invoke_stage(plugin, pipeline, pipeline.stages[0])

    paths = [f.path for f in inv.files]
    assert paths == ["deploy.sh", "verify.sh"]


def test_disabled_stage_runs_no_verbs() -> None:
    pipeline = _pipeline(["build"])
    # rebuild the stage as disabled
    disabled = _stage("build", enabled=False)
    plugin = _RecordingTarget()

    inv = invoke_stage(plugin, pipeline, disabled)

    assert plugin.calls == []
    assert inv.results == ()
    assert inv.commands == ()


def test_ci_only_stage_invokes_nothing_on_plugin() -> None:
    pipeline = _pipeline(["security"])
    plugin = _RecordingTarget()

    inv = invoke_stage(plugin, pipeline, pipeline.stages[0])

    assert plugin.calls == []
    assert inv.verbs == ()


def test_settings_are_passed_through_to_each_verb_context() -> None:
    pipeline = _pipeline(["test"])

    captured: list[str] = []

    class _Capturing(_RecordingTarget):
        def _run(self, ctx: VerbContext) -> VerbResult:  # type: ignore[override]
            captured.append(ctx.settings.get("region", "<none>"))
            return super()._run(ctx)

        lint = _run
        test = _run
        build = _run
        package = _run
        deploy = _run
        verify = _run

    invoke_stage(_Capturing(), pipeline, pipeline.stages[0], settings={"region": "uksouth"})
    assert captured == ["uksouth", "uksouth"]


# ---------------------------------------------------------------------------
# Platform-agnosticism — the SAME core path drives two different shapes
# ---------------------------------------------------------------------------
def test_same_core_path_drives_two_different_platforms() -> None:
    """The core invokes identical verb sequences; only VerbResults differ (R3.4)."""
    pipeline = _pipeline(["build"])

    ecs_like = _RecordingTarget()
    databricks_like = _ImagelessTarget()

    ecs_inv = invoke_stage(ecs_like, pipeline, pipeline.stages[0])
    dbx_inv = invoke_stage(databricks_like, pipeline, pipeline.stages[0])

    # Both platforms were asked for the SAME verbs in the SAME order ...
    assert ecs_inv.verbs == dbx_inv.verbs == (StageVerb.BUILD, StageVerb.PACKAGE)
    assert databricks_like.calls == [StageVerb.BUILD, StageVerb.PACKAGE]

    # ... but the composed commands differ because the plugins differ, not the core.
    assert ecs_inv.commands == ("build:build", "package:build")
    # databricks package is a no-op -> only the build command survives
    assert dbx_inv.commands == ("databricks-build",)


# ---------------------------------------------------------------------------
# invoke_pipeline — declared order across all stages
# ---------------------------------------------------------------------------
def test_invoke_pipeline_walks_stages_in_declared_order() -> None:
    pipeline = _pipeline(["test", "security", "build", "deploy-prod"])
    plugin = _RecordingTarget()

    invocations = invoke_pipeline(plugin, pipeline)

    assert isinstance(invocations[0], StageInvocation)
    assert [inv.stage.id for inv in invocations] == [
        "test",
        "security",
        "build",
        "deploy-prod",
    ]
    # full verb trace proves order across stages, with security contributing none
    assert plugin.calls == [
        (StageVerb.LINT, "test"),
        (StageVerb.TEST, "test"),
        (StageVerb.BUILD, "build"),
        (StageVerb.PACKAGE, "build"),
        (StageVerb.DEPLOY, "deploy-prod"),
        (StageVerb.VERIFY, "deploy-prod"),
    ]
