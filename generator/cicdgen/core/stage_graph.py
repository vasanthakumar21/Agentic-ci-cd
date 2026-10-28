"""Canonical stage dependency graph.

Formalises the stage dependency graph in the core: it validates declared
``depends_on`` edges, detects cycles, and produces a deterministic topological
order. It **extends** — does not replace —:class:`cicdgen.plugins.base.StageGraph`:
``base.StageGraph`` is the vendor-neutral *view* a CI plugin renders; this module
is the pure graph *logic* the dispatcher runs to decide what order those stages
go into that view. Everything here is **platform-blind**: it knows nothing about
verbs, vendors, or platforms — only stage ids and their ``depends_on`` edges.

Three pure functions make up the gate:

 *:func:`validate_dependencies` — every ``depends_on`` id MUST name a declared
 stage, else raise naming the offending ``(stage, missing-dep)`` edge. Mirrors
 the IR's cross-field style (``stage_environment_must_be_declared``).
 *:func:`detect_cycles` — raise naming a concrete cycle (``a -> b -> a``) if the
 graph is not a DAG.
 *:func:`topological_order` — return a permutation of the stages in which every
 dependency precedes its dependent. **Deterministic tie-break:** among the
 nodes whose dependencies are all satisfied ("ready"), pick the one earliest
 in the IR's declared order, so the result is byte-stable for the same IR.
 With no ``depends_on`` anywhere the output equals the declared order (a
 pipeline that does not use ``depends_on`` is never reordered).

The dispatcher composes the three in order — ``validate_dependencies`` →
``detect_cycles`` → ``topological_order`` — BEFORE any render (fail-fast), then
hands the ordered stages to the CI plugin via :func:`build_ordered_stage_graph`.
CI plugins only *orchestrate* the order they are given; the graph logic is the
core's, identical for every platform.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from cicdgen.ir.models import Pipeline, Stage
from cicdgen.plugins.base import (
    Capability,
    StageGraph,
    StageVerb,
    TargetPlatformPlugin,
    VerbContext,
    VerbResult,
)


class StageGraphError(ValueError):
    """Raised when the declared stage dependency graph is invalid.

 Fail-fast and precise, mirroring the IR's cross-field errors: the message
 names the offending edge (undeclared dependency) or the concrete cycle
 (``a -> b -> a``). Subclasses :class:`ValueError` so it sits alongside the
 pydantic/IR validation errors the core already treats as "bad IR".
 """


class UndeclaredDependencyError(StageGraphError):
    """A ``depends_on`` id does not name any declared stage."""


class CyclicDependencyError(StageGraphError):
    """The ``depends_on`` edges form a cycle — the graph is not a DAG."""


def _stages_tuple(stages: Iterable[Stage]) -> tuple[Stage, ...]:
    """Normalise any stage iterable to a tuple without copying stages."""
    return tuple(stages)


def validate_dependencies(stages: Sequence[Stage]) -> None:
    """Fail fast if any ``depends_on`` id does not name a declared stage.

 Mirrors the IR's ``stage_environment_must_be_declared`` cross-field rule: a
 typo in a dependency surfaces here, before any render, naming the offending
 ``(stage, missing-dep)`` edge and the declared ids for context.

 Args:
 stages: the declared stages, in declared order.

 Raises:
 UndeclaredDependencyError: naming the first offending edge (ordered by
 declared stage, then by declared dependency order) and the set of
 declared stage ids.
 """
    stages = _stages_tuple(stages)
    declared = {stage.id for stage in stages}
    for stage in stages:
        for dep in stage.depends_on:
            if dep not in declared:
                known = sorted(declared) if declared else "(none declared)"
                raise UndeclaredDependencyError(
                    f"stage '{stage.id}' depends_on '{dep}', which is not a "
                    f"declared stage. Declared stages: {known}."
                )


def detect_cycles(stages: Sequence[Stage]) -> None:
    """Fail fast if the ``depends_on`` edges form a cycle (graph is not a DAG).

 An edge ``u -> v`` means "``v`` depends on ``u``", i.e. ``u`` must run before
 ``v``. A cycle means a set of stages each (transitively) wait on each other
 and so can never run. The message names a CONCRETE cycle as a path
 (``a -> b -> a``) so the author sees exactly which edges to break.

 Assumes :func:`validate_dependencies` has already passed (every ``depends_on``
 id names a declared stage); an unknown id is ignored here rather than
 re-reported, keeping the two checks single-purpose.

 Args:
 stages: the declared stages, in declared order.

 Raises:
 CyclicDependencyError: naming a concrete cycle path.
 """
    stages = _stages_tuple(stages)
    declared = {stage.id for stage in stages}
    # dependents-of: for a stage s, the stages s depends on (edges point from a
    # dependency into its dependent when we walk "must run before").
    depends_on: dict[str, tuple[str, ...]] = {
        stage.id: tuple(d for d in stage.depends_on if d in declared) for stage in stages
    }

    # Iterative DFS with a white/grey/black colouring so a back-edge reveals a
    # concrete cycle path. Nodes are visited in declared order for a deterministic
    # cycle report when more than one exists.
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {stage.id: WHITE for stage in stages}

    def _walk(start: str) -> None:
        # stack holds (node, index-into-its-deps); path is the current grey chain.
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = [start]
        colour[start] = GREY
        while stack:
            node, idx = stack[-1]
            deps = depends_on[node]
            if idx < len(deps):
                stack[-1] = (node, idx + 1)
                nxt = deps[idx]
                if colour[nxt] == GREY:
                    # Back-edge: nxt is on the current path -> cycle from nxt..node,nxt.
                    cut = path.index(nxt)
                    cycle = [*path[cut:], nxt]
                    raise CyclicDependencyError(
                        f"stage dependency cycle detected: {' -> '.join(cycle)}. "
                        f"depends_on edges must form a DAG."
                    )
                if colour[nxt] == WHITE:
                    colour[nxt] = GREY
                    stack.append((nxt, 0))
                    path.append(nxt)
            else:
                colour[node] = BLACK
                stack.pop()
                path.pop()

    for stage in stages:
        if colour[stage.id] == WHITE:
            _walk(stage.id)


def topological_order(stages: Sequence[Stage]) -> tuple[Stage, ...]:
    """Return the stages in a deterministic, dependency-respecting order.

 Every ``depends_on`` edge ``u -> v`` (``v`` depends on ``u``) places ``u``
 before ``v`` in the result. The tie-break is **deterministic**: among the
 "ready" nodes (all dependencies already emitted) pick the one earliest in the
 IR's declared order, so the output is byte-stable for the same IR.

 With no ``depends_on`` anywhere every node is ready from the start and the
 earliest-declared tie-break emits them in declared order — i.e. the output
 equals the input, so a pipeline that does not use ``depends_on`` is never
 reordered.

 Assumes :func:`validate_dependencies` and :func:`detect_cycles` have already
 passed (all ids declared, graph acyclic).

 Args:
 stages: the declared stages, in declared order.

 Returns:
 A permutation of ``stages`` (same objects) honouring every dependency,
 with the earliest-declared deterministic tie-break.
 """
    stages = _stages_tuple(stages)
    order_index = {stage.id: i for i, stage in enumerate(stages)}
    by_id = {stage.id: stage for stage in stages}
    declared = set(by_id)

    # Remaining unsatisfied dependency count per stage (ignore ids not declared;
    # validate_dependencies guarantees there are none by the time we are called).
    remaining: dict[str, int] = {
        sid: sum(1 for d in by_id[sid].depends_on if d in declared) for sid in by_id
    }

    emitted: list[Stage] = []
    emitted_ids: set[str] = set()
    # Candidate set rebuilt each step is simplest and deterministic; stage counts
    # here are small (pipeline stages), so O(n^2) is fine and keeps the tie-break
    # obviously correct: always the earliest-declared ready node.
    while len(emitted) < len(stages):
        ready = sorted(
            (
                sid
                for sid in by_id
                if sid not in emitted_ids and remaining[sid] == 0
            ),
            key=lambda sid: order_index[sid],
        )
        # detect_cycles has already run, so `ready` is non-empty until done.
        nxt = ready[0]
        emitted.append(by_id[nxt])
        emitted_ids.add(nxt)
        for sid in by_id:
            if sid not in emitted_ids and nxt in by_id[sid].depends_on:
                remaining[sid] -= 1

    return tuple(emitted)


def ordered_stages(stages: Sequence[Stage]) -> tuple[Stage, ...]:
    """Run the full dependency gate and return the topologically ordered stages.

 Composes the three checks in the fail-fast order the dispatcher uses:
:func:`validate_dependencies` →:func:`detect_cycles` →:func:`topological_order`.

 Args:
 stages: the declared stages, in declared order.

 Returns:
 The topologically ordered stages (declared order when no ``depends_on``).

 Raises:
 UndeclaredDependencyError: a ``depends_on`` id names no declared stage.
 CyclicDependencyError: the ``depends_on`` edges form a cycle.
 """
    stages = _stages_tuple(stages)
    validate_dependencies(stages)
    detect_cycles(stages)
    return topological_order(stages)


def build_ordered_stage_graph(
    pipeline: Pipeline,
    *,
    stage_commands: Mapping[str, tuple[str, ...]] | None = None,
) -> StageGraph:
    """Build the vendor-neutral :class:`StageGraph` with stages topologically ordered.

 The dispatcher invokes this instead of the naive ``StageGraph.from_pipeline``
 (which preserves declared order verbatim): it runs :func:`ordered_stages` so a
 pipeline using ``depends_on`` is handed to the CI plugin in a dependency-
 respecting order, while a pipeline that does not use ``depends_on`` is
 byte-identically unchanged.

 Args:
 pipeline: a validated IR.
 stage_commands: optional per-stage-id target verb commands the core
 composed at dispatch time (the ``build``/``package``/``deploy``/… shell
 steps the chosen target plugin produced). Threaded onto the graph so the
 CI plugin can render them as steps and the generated pipeline actually
 runs the platform's work. ``None``/empty => the CI plugin falls back to
 ``stage.runs``.

 Returns:
 A :class:`StageGraph` carrying the IR, the ordered stages, and (optionally)
 the per-stage target verb commands.

 Raises:
 UndeclaredDependencyError: a ``depends_on`` id names no declared stage.
 CyclicDependencyError: the ``depends_on`` edges form a cycle.
 """
    stages = ordered_stages(pipeline.stages)
    return StageGraph(
        pipeline=pipeline,
        stages=stages,
        stage_commands=dict(stage_commands) if stage_commands else {},
    )


# ---------------------------------------------------------------------------
# Artifact promotion — build-once / promote-across-environments
# ---------------------------------------------------------------------------
#: The ``VerbContext.settings`` key under which the core hands the resolved,
#: immutable artifact reference to a target plugin's ``package`` (which produces
#: it) and every ``deploy`` (which consumes it). Passing it this way keeps the
#: verb contract unchanged: what differs is only the ORDER the core composes verbs
#: and the shared reference it threads through them. The VALUE is a by-reference
#: shell variable (never a digest/key literal), so nothing account- or
#: image-specific is ever inlined.
PROMOTION_ARTIFACT_REF_KEY = "promotion_artifact_ref"

#: Companion key carrying the logical artifact name from ``promotion.artifact`` so
#: a plugin can name the reference deterministically if it wishes. Also by-name,
#: never a value.
PROMOTION_ARTIFACT_NAME_KEY = "promotion_artifact"


class PromotionError(ValueError):
    """Raised when a promotion-enabled IR cannot be composed.

 Fail-fast, value-free: the message names the missing capability or the
 structural problem, never a secret value. Subclasses :class:`ValueError`
 so it sits alongside the IR/stage-graph validation errors the core already
 treats as "bad IR".
 """


def _artifact_reference(artifact: str) -> str:
    """Return the deterministic by-reference shell variable for a logical artifact.

 The reference is derived PURELY from the logical ``promotion.artifact`` name —
 no digest, image id, or account specific is ever embedded. It is stable for
 the same IR: ``service-image`` -> ``$SERVICE_IMAGE_REF``. Only
 ``[A-Za-z0-9]`` survive; every other character folds to ``_`` so the result is
 always a valid shell variable name.
 """
    safe = "".join(ch.upper() if ch.isalnum() else "_" for ch in artifact)
    return f"${safe}_REF"


@dataclass(frozen=True)
class PromotionStep:
    """One composed unit of a promotion plan: a label + the verbs it runs.

 ``environment`` is ``None`` for the single build+package step and the
 environment name for each per-environment deploy+verify step. ``results`` are
 the ``(verb, VerbResult)`` pairs in invocation order, exactly like a
:class:`~cicdgen.plugins.stage_contract.StageInvocation` but keyed to the
 promotion role rather than an IR stage.
 """

    environment: str | None
    verbs: tuple[StageVerb, ...]
    results: tuple[tuple[StageVerb, VerbResult], ...]


@dataclass(frozen=True)
class PromotionPlan:
    """The build-once / promote-across-environments composition.

 Shape (platform-blind):

 * exactly ONE :attr:`build_step` running ``build``+``package`` that
 produces the immutable artifact reference :attr:`artifact_reference`;
 * one :class:`PromotionStep` in :attr:`deploy_steps` per environment in
 ``promotion.path`` (in that order), each running ONLY ``deploy``+
 ``verify`` and consuming the SAME :attr:`artifact_reference` (threaded
 through ``VerbContext.settings``); no deploy step re-runs build/package.

 Every environment therefore deploys the identical reference.
 """

    artifact: str
    artifact_reference: str
    build_step: PromotionStep
    deploy_steps: tuple[PromotionStep, ...]

    @property
    def all_steps(self) -> tuple[PromotionStep, ...]:
        """The build step followed by every deploy step, in promote order."""
        return (self.build_step, *self.deploy_steps)

    def deploy_references(self) -> tuple[str, ...]:
        """The artifact reference each deploy step received (one per environment).

 All N deploys reference the SAME value.
 """
        return tuple(self.artifact_reference for _ in self.deploy_steps)


#: The verbs the single build step runs (produces the promotable artifact once).
PROMOTION_BUILD_VERBS: tuple[StageVerb, ...] = (StageVerb.BUILD, StageVerb.PACKAGE)
#: The verbs each per-environment step runs (consume the pre-built reference).
PROMOTION_DEPLOY_VERBS: tuple[StageVerb, ...] = (StageVerb.DEPLOY, StageVerb.VERIFY)


def _promotion_stage(pipeline: Pipeline, *, environment: str | None) -> Stage:
    """Pick the IR stage to carry into a promotion step's verb contexts.

 Promotion reuses the real IR stages so a verb still sees ``stage.runs`` and the
 rest of its context. For the build step we prefer a build-shaped declared stage
 (so ``build``/``package`` honour any ``runs``); for a deploy step we prefer the
 declared stage that targets that environment. When no suitable stage is
 declared we fall back to the first stage — the composition still holds, it just
 carries less stage-specific context. This never reorders or mutates the IR.
 """
    stages = tuple(pipeline.stages)
    if environment is None:
        for stage in stages:
            sid = stage.id.casefold()
            if "build" in sid or "package" in sid or "image" in sid:
                return stage
        return stages[0]
    for stage in stages:
        if stage.environment == environment:
            return stage
    return stages[0]


def compose_promotion_plan(
    pipeline: Pipeline,
    target_plugin: TargetPlatformPlugin,
    *,
    settings: Mapping[str, str] | None = None,
) -> PromotionPlan:
    """Compose the build-once / promote-across-environments plan.

 Platform-blind: it invokes the target plugin only through the uniform verb
 contract and never inspects ``pipeline.target_platform`` or the plugin's name.
 It shapes the composition so:

 1. the chosen ``target_platform`` plugin MUST declare
:attr:`Capability.ARTIFACT_PROMOTION` (else :class:`PromotionError`,
 fail-fast — this mirrors the capability gate so promotion cannot be
 requested against a target that would silently rebuild per env);
 2. ONE ``build``+``package`` pair runs, producing the immutable artifact
 reference (:func:`_artifact_reference`, by-reference only);
 3. each environment in ``promotion.path`` runs ONLY ``deploy``+``verify``,
 all consuming the SAME reference threaded through
 ``VerbContext.settings`` (no environment re-runs build/package).

 Args:
 pipeline: a validated IR whose ``promotion`` block is present and enabled.
 target_plugin: the loaded ``target_platform`` plugin.
 settings: resolved, non-secret knobs forwarded into every verb context
 (region, names, …); the promotion reference is added on top under
:data:`PROMOTION_ARTIFACT_REF_KEY` without mutating the caller's map.

 Returns:
 A :class:`PromotionPlan` carrying the single build step and the per-
 environment deploy steps, all sharing one artifact reference.

 Raises:
 PromotionError: if ``promotion`` is absent/disabled, or the target plugin
 does not declare ``ARTIFACT_PROMOTION``.
 """
    promotion = pipeline.promotion
    if promotion is None or not promotion.enabled:
        raise PromotionError(
            "compose_promotion_plan called without an enabled 'promotion' block; "
            "the core only composes a promotion plan when promotion.enabled is true."
        )

    if Capability.ARTIFACT_PROMOTION not in target_plugin.capabilities:
        raise PromotionError(
            f"artifact promotion was requested but the target_platform plugin "
            f"'{target_plugin.name}' does not declare the "
            f"'{Capability.ARTIFACT_PROMOTION.value}' capability; it would have to "
            f"rebuild the artifact per environment. Choose a promotion-capable "
            f"target or remove the 'promotion' block."
        )

    artifact_reference = _artifact_reference(promotion.artifact)

    # Thread the shared reference + logical name through settings WITHOUT mutating
    # the caller's mapping. The package verb emits this reference; every deploy
    # verb consumes it — so all environments deploy the identical artifact.
    promo_settings: dict[str, str] = dict(settings or {})
    promo_settings[PROMOTION_ARTIFACT_REF_KEY] = artifact_reference
    promo_settings[PROMOTION_ARTIFACT_NAME_KEY] = promotion.artifact

    # 1. The single build+package step (produces the artifact once).
    build_stage = _promotion_stage(pipeline, environment=None)
    build_results = _run_verbs(
        target_plugin, pipeline, build_stage, PROMOTION_BUILD_VERBS, promo_settings
    )
    build_step = PromotionStep(
        environment=None, verbs=PROMOTION_BUILD_VERBS, results=build_results
    )

    # 2. One deploy+verify step per environment in the promote path, each given
    # the SAME artifact reference (no rebuild).
    deploy_steps: list[PromotionStep] = []
    for env_name in promotion.path:
        deploy_stage = _promotion_stage(pipeline, environment=env_name)
        env_settings = {**promo_settings, "promotion_environment": env_name}
        deploy_results = _run_verbs(
            target_plugin, pipeline, deploy_stage, PROMOTION_DEPLOY_VERBS, env_settings
        )
        deploy_steps.append(
            PromotionStep(
                environment=env_name,
                verbs=PROMOTION_DEPLOY_VERBS,
                results=deploy_results,
            )
        )

    return PromotionPlan(
        artifact=promotion.artifact,
        artifact_reference=artifact_reference,
        build_step=build_step,
        deploy_steps=tuple(deploy_steps),
    )


def _run_verbs(
    plugin: TargetPlatformPlugin,
    pipeline: Pipeline,
    stage: Stage,
    verbs: tuple[StageVerb, ...],
    settings: Mapping[str, str],
) -> tuple[tuple[StageVerb, VerbResult], ...]:
    """Invoke a fixed verb sequence for one stage via the uniform verb contract.

 Mirrors ``stage_contract.invoke_stage`` but with an EXPLICIT verb list (the
 promotion composition decides the sequence, not the stage-id heuristics) and a
 stable :class:`VerbContext` per verb. Platform-blind: it only calls
:meth:`TargetPlatformPlugin.run_verb`.
 """
    results: list[tuple[StageVerb, VerbResult]] = []
    for verb in verbs:
        ctx = VerbContext(
            pipeline=pipeline, stage=stage, verb=verb, settings=settings
        )
        results.append((verb, plugin.run_verb(ctx)))
    return tuple(results)
