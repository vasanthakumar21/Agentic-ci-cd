"""Core dispatch / orchestration — the vendor- & platform-agnostic conductor.

Given a *validated*:class:`Pipeline` IR this module loads the two named plugins
and runs them in a fixed order, producing one structured result. It loads the
CI-vendor plugin + the target plugin and renders, holding **no** vendor/platform
knowledge: there is not one ``if ci_vendor == …`` or ``if target_platform == …``
branch here. The only names it ever touches are the ``ci_vendor`` /
``target_platform`` strings the IR hands it, which it passes straight to the
loader.

Orchestration order (fixed, platform-blind)
-------------------------------------------
1. **Load** the target-platform plugin (``plugins/target/<target_platform>/``)
 and the CI-vendor plugin (``plugins/ci/<ci_vendor>/``) by name.
2. **Build** the vendor-neutral :class:`StageGraph` from the IR.
3. **Invoke** the target plugin uniformly via the stage-verb contract
 (:func:`invoke_pipeline`) to get the per-stage verb results — the shell steps
 + support files each verb produces. The core never interprets these; it only
 composes them.
4. **Emit** the pipeline files by handing the stage graph to the CI-vendor
 plugin's :meth:`emit_pipeline_files` — the CI plugin arranges order/gates and
 calls the verbs; it supplies no business logic.
5. **Collect** the target plugin's :meth:`supply_iac` (the backing Terraform /
 API-driver IaC).

The result bundles every :class:`GeneratedFile` from all three sources (CI
pipeline files, per-verb support files, IaC files) so the generator core can
write them + record them in ``generated.manifest.json`` and remove them cleanly
on an axis swap.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from cicdgen.ir.models import Pipeline
from cicdgen.plugins.base import (
    CIVendorPlugin,
    GeneratedFile,
    IaCArtifacts,
    PluginFamily,
    TargetPlatformPlugin,
)
from cicdgen.plugins.loader import Registry, load_plugin
from cicdgen.plugins.stage_contract import StageInvocation, invoke_pipeline

if TYPE_CHECKING:
    from cicdgen.core.stage_graph import PromotionPlan


@dataclass(frozen=True)
class DispatchResult:
    """The full, structured outcome of dispatching one IR through its plugins.

 Everything crossing the plugin boundary ends up here so the generator core
 has a single object to write + manifest. Files are grouped by
 origin for a reviewable diff, and :meth:`all_files` is the de-duplicated flat
 list the writer consumes.
 """

    #: Which plugins actually ran (their names == the IR axes).
    ci_vendor: str
    target_platform: str

    #: Files the CI-vendor plugin emitted (pipeline YAML, etc.).
    ci_files: tuple[GeneratedFile, ...]
    #: Support files the target verbs emitted (Dockerfile, job JSON, …).
    verb_files: tuple[GeneratedFile, ...]
    #: The backing IaC the target plugin supplied (Terraform / API driver).
    iac: IaCArtifacts
    #: Per-stage verb results, in declared order (orchestration record).
    stage_invocations: tuple[StageInvocation, ...]

    #: The references actually used, exposed for later phases.
    ci_plugin: CIVendorPlugin = field(repr=False)
    target_plugin: TargetPlatformPlugin = field(repr=False)

    #: The build-once / promote-across-environments composition, present only when
    #: the IR requested an enabled ``promotion`` block. ``None`` for every
    #: non-promotion pipeline. When set it carries the single build+package step
    #: and the per-environment deploy+verify steps, all sharing one immutable
    #: artifact reference.
    promotion_plan: PromotionPlan | None = None

    def all_files(self) -> tuple[GeneratedFile, ...]:
        """Every generated file, de-duplicated by path (last-write-wins).

 Order: CI pipeline files, then verb support files, then IaC files. A path
 emitted by more than one source resolves to the last writer, so the core
 never records a file twice in the manifest.
 """
        by_path: dict[str, GeneratedFile] = {}
        for f in (*self.ci_files, *self.verb_files, *self.iac.files):
            by_path[f.path] = f
        return tuple(by_path.values())


def dispatch(
    pipeline: Pipeline,
    *,
    plugins_root: Path | str | None = None,
    registry: Registry | None = None,
    settings: Mapping[str, str] | None = None,
) -> DispatchResult:
    """Load the two named plugins and orchestrate one generation pass.

 This is the core's single entry point from a validated IR to a bundle of
 generated artifacts. It is completely platform-agnostic: it reads only
 ``pipeline.ci_vendor`` / ``pipeline.target_platform`` and forwards them to the
 loader — every vendor/platform follows the identical code path.

 Args:
 pipeline: a *validated* IR (schema + plugin-existence already checked by
:mod:`cicdgen.ir`).
 plugins_root: root containing ``ci/`` and ``target/`` family folders;
 defaults to the repo's ``plugins/``. Injectable for tests.
 registry: optional in-memory ``(family, name) -> plugin`` override that
 bypasses the filesystem (tests / an agent injecting plugins).
 settings: resolved, non-secret knobs forwarded into every verb context
 (region, project name, …); secret *references* are emitted by the
 verbs, never values.

 Returns:
 A :class:`DispatchResult` carrying every generated file (CI + verb + IaC),
 the per-stage verb results, and the two plugin instances that ran.

 Raises:
 cicdgen.plugins.loader.PluginLoadError: if either named plugin cannot be
 loaded into a valid instance.
 """
    ci_vendor = str(pipeline.ci_vendor)
    target_platform = str(pipeline.target_platform)

    # 1. Load both plugins by name — no vendor/platform literals here.
    target_plugin = load_plugin(
        PluginFamily.TARGET_PLATFORM,
        target_platform,
        plugins_root=plugins_root,
        registry=registry,
    )
    ci_plugin = load_plugin(
        PluginFamily.CI_VENDOR,
        ci_vendor,
        plugins_root=plugins_root,
        registry=registry,
    )
    assert isinstance(target_plugin, TargetPlatformPlugin)  # guaranteed by loader
    assert isinstance(ci_plugin, CIVendorPlugin)  # guaranteed by loader

    # 1b. Capability gate: now that both plugins are loaded — so the union of
    # their declared capabilities is known — fail fast BEFORE any render if the IR
    # structure requires a capability neither axis provides. The check is
    # platform-blind: it reads only the vendor-neutral IR + the plugins' declared
    # data, never their names. Imported lazily to avoid an import cycle (the core
    # package imports this module during its own initialisation).
    from cicdgen.core.capabilities import validate_capabilities

    validate_capabilities(pipeline, ci_plugin, target_plugin)

    # 1c. Canonical stage-graph gate: validate the declared depends_on edges,
    # reject cycles, and compute a DETERMINISTIC topological order — all BEFORE any
    # render (fail-fast). The graph logic is the core's and is platform-blind: it
    # reads only stage ids + their depends_on edges, never a vendor/platform name.
    # A pipeline that declares no depends_on is left byte-identically in declared
    # order. Imported lazily for the same reason as the capability gate above
    # (import cycle).
    from cicdgen.core.stage_graph import build_ordered_stage_graph

    # 2. Drive the target plugin uniformly to get per-stage verb results FIRST, so
    # the stage graph can carry each stage's composed verb commands into the CI
    # plugin (the CI pipeline invokes the verbs and only arranges them, never
    # inspecting what they do). Platform-blind — same call for every target.
    stage_invocations = invoke_pipeline(target_plugin, pipeline, settings=settings)
    stage_commands = {
        inv.stage.id: inv.commands
        for inv in stage_invocations
        if inv.commands
    }

    # 2b. Build the vendor-neutral stage graph in topological (execution) order,
    # carrying the per-stage verb commands so the CI plugin renders them as steps.
    graph = build_ordered_stage_graph(pipeline, stage_commands=stage_commands)

    # 2b. Artifact-promotion composition: ONLY when the IR requests an enabled
    # promotion block. The composition is platform-blind (it drives the target
    # plugin through the uniform verb contract) and shapes one build+package pair
    # plus one deploy+verify per environment, all sharing one immutable artifact
    # reference — no environment rebuilds. When promotion is absent this stays None
    # and the per-stage flow below is byte-identically unchanged. Imported lazily
    # to avoid an import cycle (the core package imports this module during its own
    # initialisation).
    promotion_plan = None
    promotion = pipeline.promotion
    if promotion is not None and promotion.enabled:
        from cicdgen.core.stage_graph import compose_promotion_plan

        promotion_plan = compose_promotion_plan(
            pipeline, target_plugin, settings=settings
        )

    # 3. Hand the stage graph (now carrying per-stage verb commands) to the CI
    # plugin to emit the pipeline files. The CI plugin renders those commands as
    # the stage's steps, falling back to stage.runs where a stage has no verbs.
    ci_files = tuple(ci_plugin.emit_pipeline_files(graph))

    # 5. Collect the target plugin's backing IaC.
    iac = target_plugin.supply_iac(pipeline)

    verb_files = _compose_verb_files(stage_invocations)

    return DispatchResult(
        ci_vendor=ci_vendor,
        target_platform=target_platform,
        ci_files=ci_files,
        verb_files=verb_files,
        iac=iac,
        stage_invocations=stage_invocations,
        ci_plugin=ci_plugin,
        target_plugin=target_plugin,
        promotion_plan=promotion_plan,
    )


def _compose_verb_files(
    invocations: tuple[StageInvocation, ...],
) -> tuple[GeneratedFile, ...]:
    """Flatten every verb's support files across all stages, de-duped by path."""
    by_path: dict[str, GeneratedFile] = {}
    for inv in invocations:
        for f in inv.files:
            by_path[f.path] = f
    return tuple(by_path.values())
