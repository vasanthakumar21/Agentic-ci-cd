"""The stable stage-verb contract the core invokes uniformly.

The verb set is defined by :class:`~cicdgen.plugins.base.StageVerb` with the
per-platform implementations in
:class:`~cicdgen.plugins.base.TargetPlatformPlugin`. This module defines the
*contract that sits between an IR stage and those verbs*: the single,
platform-agnostic mechanism the core uses to turn one declared stage into an
ordered sequence of verbs and then compose the per-verb results.

Why this belongs in the core
-----------------------------
* The stage interface (the six verbs) is stable and invoked *uniformly* by the
 generated pipeline. The core therefore owns a single, fixed rule for "which
 verbs does this stage run, and in what order?" so every platform is driven the
 same way.
* Generated CI files only *orchestrate and call* verbs; they hold no platform
 business logic. The mapping here is pure orchestration data: it decides verb
 *sequence*, never verb *behaviour* (behaviour is the plugin's job).
* Adding a plugin never touches the core. This module contains **zero**
 ``if ci_vendor == …`` / ``if target_platform == …`` branches. It talks only to
 the abstract :class:`TargetPlatformPlugin` via its uniform
:meth:`~cicdgen.plugins.base.TargetPlatformPlugin.run_verb` dispatcher, so it
 works unchanged for aws-ecs, databricks, or any plugin authored later.

The stage → verbs policy
------------------------
Stage ids are *user-defined* (``test``, ``security``, ``build``, ``deploy-dev``,
``deploy-prod`` …), so the contract cannot hard-code a closed list of ids. Instead
it maps a stage to verbs by **stage semantics**, expressed as declarative data
(:data:`STAGE_VERB_POLICY`) rather than control flow:

 * a *test-shaped* stage -> lint, test
 * a *build-shaped* stage -> build, package
 * a *deploy-shaped* stage-> deploy, verify
 * a *security* stage -> (no target verbs; handled by CI scans)

Classification is by a documented set of substrings on the stage id (e.g. any id
containing ``deploy`` or ``release`` is deploy-shaped). This keeps the rule a small
lookup table a human can read in the steering doc, and it is identical for every
platform — exactly what "invoked uniformly, independent of platform" means.

A stage may also pin its verbs explicitly via ``settings`` the caller passes
(:func:`verbs_for_stage` accepts an ``explicit`` override), so a client whose stage
ids don't match the heuristics is never stuck — the override is still platform-
agnostic data, not code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from cicdgen.ir.models import Pipeline, Stage
from cicdgen.plugins.base import (
    GeneratedFile,
    StageVerb,
    TargetPlatformPlugin,
    VerbContext,
    VerbResult,
)

# ---------------------------------------------------------------------------
# The declarative stage -> verbs policy (data, not branching)
# ---------------------------------------------------------------------------
#: Ordered verb sequences for each *stage shape*. The core composes verbs in this
#: exact order so the generated pipeline runs them deterministically. These are
#: the platform-neutral sequences; what each verb *does* is the plugin's job.
VERBS_FOR_TEST_STAGE: tuple[StageVerb, ...] = (StageVerb.LINT, StageVerb.TEST)
VERBS_FOR_BUILD_STAGE: tuple[StageVerb, ...] = (StageVerb.BUILD, StageVerb.PACKAGE)
VERBS_FOR_DEPLOY_STAGE: tuple[StageVerb, ...] = (StageVerb.DEPLOY, StageVerb.VERIFY)

#: Stage-id substring -> verb sequence, tried in order. The FIRST matching entry
#: wins, so more specific shapes (deploy) are listed before generic ones. This is
#: the whole of the core's platform-agnostic routing knowledge; a reviewer can read
#: it top-to-bottom. Matching is case-insensitive on the stage id (ids like
#: ``deploy-prod`` / ``build`` / ``test``).
STAGE_VERB_POLICY: tuple[tuple[tuple[str, ...], tuple[StageVerb, ...]], ...] = (
    # deploy-shaped: deploy then verify
    (("deploy", "release", "rollout", "promote"), VERBS_FOR_DEPLOY_STAGE),
    # build-shaped: build then package/publish
    (("build", "package", "publish", "image", "compile"), VERBS_FOR_BUILD_STAGE),
    # test-shaped: lint then test
    (("test", "lint", "check", "verify", "qa", "unit", "integration"), VERBS_FOR_TEST_STAGE),
)

#: Stage ids that run *no* target-platform verbs. A security/scan stage is expressed
#: entirely by the CI vendor (SAST/secrets/dependency scans); it has no
#: platform verb, so the uniform contract yields an empty verb list for it rather
#: than guessing. Matching is by substring, case-insensitive.
NON_VERB_STAGE_MARKERS: tuple[str, ...] = ("security", "scan", "approval", "notify")


def verbs_for_stage(
    stage: Stage,
    *,
    explicit: Sequence[StageVerb] | None = None,
) -> tuple[StageVerb, ...]:
    """Return the ordered verbs a stage triggers — the core's uniform rule.

 This is the single place the core decides "stage -> verbs". It is pure,
 deterministic, and platform-agnostic: it inspects only the vendor-neutral IR
 stage, never the plugin or the platform.

 Resolution order:

 1. ``explicit`` — if the caller pins verbs (e.g. from a future IR ``verbs:``
 field or a client override), those win verbatim. Still data, not code.
 2. a :data:`NON_VERB_STAGE_MARKERS` match -> ```` (no target verbs; CI-only).
 3. the first matching :data:`STAGE_VERB_POLICY` entry by stage-id substring.
 4. fallback -> ```` (an id the policy doesn't recognise contributes no verbs
 rather than silently running the wrong ones; the caller/steering can add an
 ``explicit`` mapping).

 Args:
 stage: the validated IR stage.
 explicit: optional caller-supplied verb sequence that overrides the policy.

 Returns:
 The verbs to invoke for this stage, in execution order (possibly empty).
 """
    if explicit is not None:
        return tuple(explicit)

    stage_id = stage.id.casefold()

    if any(marker in stage_id for marker in NON_VERB_STAGE_MARKERS):
        return ()

    for markers, verbs in STAGE_VERB_POLICY:
        if any(marker in stage_id for marker in markers):
            return verbs

    return ()


# ---------------------------------------------------------------------------
# Result of invoking a whole stage (one composed unit the CI plugin orchestrates)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StageInvocation:
    """The composed outcome of running every verb mapped to one stage.

 This is what the core hands onward: for a
 single IR stage it carries, in order, each verb that ran and the
:class:`~cicdgen.plugins.base.VerbResult` that verb produced, plus the flat,
 de-duplicated commands and support files ready to drop into the pipeline.

 The CI-vendor plugin only *orchestrates* these — stage order, gates, triggers —
 and never inspects what the commands do.
 """

    stage: Stage
    #: (verb, result) pairs in the exact order the verbs were invoked.
    results: tuple[tuple[StageVerb, VerbResult], ...]

    @property
    def verbs(self) -> tuple[StageVerb, ...]:
        """The verbs that ran, in order."""
        return tuple(verb for verb, _ in self.results)

    @property
    def commands(self) -> tuple[str, ...]:
        """All verb commands concatenated in verb order (empty results drop out)."""
        out: list[str] = []
        for _verb, result in self.results:
            out.extend(result.commands)
        return tuple(out)

    @property
    def files(self) -> tuple[GeneratedFile, ...]:
        """All support files from every verb, in verb order, de-duplicated by path.

 A later verb emitting the same path as an earlier one is treated as the
 authoritative version (last-write-wins), so composing verbs never writes a
 file twice into the manifest.
 """
        by_path: dict[str, GeneratedFile] = {}
        for _verb, result in self.results:
            for f in result.files:
                by_path[f.path] = f
        return tuple(by_path.values())


# ---------------------------------------------------------------------------
# The uniform invocation helper (the core's single call-site into plugins)
# ---------------------------------------------------------------------------
def invoke_stage(
    plugin: TargetPlatformPlugin,
    pipeline: Pipeline,
    stage: Stage,
    *,
    settings: Mapping[str, str] | None = None,
    explicit_verbs: Sequence[StageVerb] | None = None,
) -> StageInvocation:
    """Run every verb a stage maps to, uniformly, and compose the results.

 This is the one function the core uses to drive a stage. It is deliberately
 platform-blind: it resolves the verb sequence with :func:`verbs_for_stage`,
 builds a stable :class:`~cicdgen.plugins.base.VerbContext` per verb, and calls
 the plugin's uniform :meth:`~cicdgen.plugins.base.TargetPlatformPlugin.run_verb`
 dispatcher. There is no platform/vendor branching here — swap aws-ecs for
 databricks and this function is unchanged; only the ``VerbResult`` contents
 differ.

 A disabled stage (``stage.enabled is False``) contributes no verbs, matching the
 IR's per-stage enable/disable knob without the core special-casing it anywhere
 else.

 Args:
 plugin: the selected target-platform plugin (any concrete subclass).
 pipeline: the validated IR (carried into every ``VerbContext``).
 stage: the stage to invoke.
 settings: resolved, non-secret knobs passed through to each verb context
 (region, project name, …); secret *references* are emitted by the verb,
 never values.
 explicit_verbs: optional override forwarded to :func:`verbs_for_stage`.

 Returns:
 A :class:`StageInvocation` pairing each invoked verb with its result, in
 order, plus composed commands/files.
 """
    resolved_settings = dict(settings or {})

    if not stage.enabled:
        return StageInvocation(stage=stage, results=())

    verbs = verbs_for_stage(stage, explicit=explicit_verbs)

    results: list[tuple[StageVerb, VerbResult]] = []
    for verb in verbs:
        ctx = VerbContext(
            pipeline=pipeline,
            stage=stage,
            verb=verb,
            settings=resolved_settings,
        )
        results.append((verb, plugin.run_verb(ctx)))

    return StageInvocation(stage=stage, results=tuple(results))


def invoke_pipeline(
    plugin: TargetPlatformPlugin,
    pipeline: Pipeline,
    *,
    settings: Mapping[str, str] | None = None,
) -> tuple[StageInvocation, ...]:
    """Invoke every stage of a pipeline in declared order, uniformly.

 Convenience over :func:`invoke_stage` for the whole IR: walks stages in their
 declared (execution) order and composes one :class:`StageInvocation` each. Like
:func:`invoke_stage` it contains no platform-specific logic — it is the
 core's single, uniform entry point for turning an IR + a target plugin into the
 per-stage verb results a CI-vendor plugin then arranges into pipeline files.

 Args:
 plugin: the selected target-platform plugin.
 pipeline: the validated IR.
 settings: resolved, non-secret knobs forwarded to each verb context.

 Returns:
 One :class:`StageInvocation` per stage, in declared order.
 """
    return tuple(
        invoke_stage(plugin, pipeline, stage, settings=settings)
        for stage in pipeline.stages
    )
