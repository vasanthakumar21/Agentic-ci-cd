"""Tests for artifact promotion — build-once / promote-across-environments (task 20, §11.6).

The core models build-once/promote in the IR + the stage graph: when
``promotion.enabled`` it shapes the composition so ONE ``build``+``package`` pair
produces an immutable artifact reference and each environment in ``promotion.path``
runs ONLY ``deploy``+``verify`` consuming the SAME reference (threaded through
``VerbContext.settings`` — by reference, R6). The chosen ``target_platform`` plugin
must declare ``Capability.ARTIFACT_PROMOTION`` or the 1b capability gate fails fast
(design §11.2/§11.6 wiring).

Covered here, following the project's parametrized-property convention (no
hypothesis dependency — inputs are enumerated/seeded deterministically):

    * Property 9 — promotion builds once, deploys the SAME reference per env: over
      a range of promotion-path lengths and promotion-capable targets, the composed
      plan contains exactly one ``build``+``package`` and N ``deploy``+``verify``
      steps (one per environment, in path order) that all reference the identical
      artifact reference; no environment re-runs ``build``/``package``;
    * 20.5 fail-fast unit test — requesting promotion against a target that does NOT
      declare ``ARTIFACT_PROMOTION`` (azure-functions, databricks) fails via the
      capability gate (and the direct composer) rather than silently rebuilding;
    * IR-level checks for the new ``promotion`` block (arrow-string normalisation,
      cross-field path-must-be-declared, backward-compat when absent);
    * end-to-end determinism: a promotion IR still generates byte-identically (R5.3).

The composer is driven both DIRECTLY (``compose_promotion_plan``) and THROUGH the
full dispatch path (``dispatch`` → ``DispatchResult.promotion_plan``) so the wiring
is exercised end to end. Plugins are the REAL on-disk promotion-capable targets,
enumerated dynamically where possible so no vendor/platform literal is load-bearing.
"""

from __future__ import annotations

import pytest

from cicdgen.core.capabilities import CapabilityError
from cicdgen.core.stage_graph import (
    PROMOTION_ARTIFACT_NAME_KEY,
    PROMOTION_ARTIFACT_REF_KEY,
    PROMOTION_BUILD_VERBS,
    PROMOTION_DEPLOY_VERBS,
    PromotionError,
    compose_promotion_plan,
)
from cicdgen.ir import Pipeline
from cicdgen.ir.plugins import AvailablePlugins
from cicdgen.plugins import (
    StageVerb,
    load_target_platform_plugin,
)
from cicdgen.plugins.base import Capability
from cicdgen.plugins.dispatch import dispatch

# ---------------------------------------------------------------------------
# Promotion-capable targets, discovered dynamically (no literal is load-bearing)
# ---------------------------------------------------------------------------
_CLOUD_FOR_TARGET = {
    "aws-ecs": "aws",
    "kubernetes": "gcp",
}


def _promotion_capable_targets() -> list[str]:
    """Every on-disk target plugin that declares ARTIFACT_PROMOTION (dynamic)."""
    available = AvailablePlugins.discover()
    capable: list[str] = []
    for name in sorted(available.target):
        plugin = load_target_platform_plugin(name)
        if Capability.ARTIFACT_PROMOTION in plugin.capabilities:
            capable.append(name)
    return capable


def _non_promotion_targets() -> list[str]:
    """Every on-disk target plugin that does NOT declare ARTIFACT_PROMOTION."""
    available = AvailablePlugins.discover()
    incapable: list[str] = []
    for name in sorted(available.target):
        plugin = load_target_platform_plugin(name)
        if Capability.ARTIFACT_PROMOTION not in plugin.capabilities:
            incapable.append(name)
    return incapable


_CAPABLE_TARGETS = _promotion_capable_targets()
_INCAPABLE_TARGETS = _non_promotion_targets()
_PATH_LENGTHS = [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# IR builders
# ---------------------------------------------------------------------------
def _env_names(n: int) -> list[str]:
    base = ["dev", "staging", "prod", "canary", "preprod", "sandbox"]
    return base[:n] if n <= len(base) else [f"env{i}" for i in range(n)]


def _cloud_for(target_platform: str) -> str:
    return _CLOUD_FOR_TARGET.get(target_platform, "aws")


def _promotion_ir(target_platform: str, n_envs: int) -> Pipeline:
    """A promotion-enabled IR with ``n_envs`` environments in the promote path."""
    envs = _env_names(n_envs)
    stages: list[dict] = [{"id": "build"}]
    for env in envs:
        stages.append({"id": f"deploy-{env}", "environment": env})
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": target_platform,
            "project": {"name": "demo"},
            "environments": [{"name": e} for e in envs],
            "stages": stages,
            "cloud": {"provider": _cloud_for(target_platform), "auth": "oidc"},
            "promotion": {
                "enabled": True,
                "artifact": "service-image",
                "path": envs,
            },
        }
    )


# ===========================================================================
# Property 9 — promotion builds once, deploys the same reference per environment
# ===========================================================================
# Feature: agentic-cicd-generator, Property 9: Artifact promotion builds once,
# deploys the same reference per environment — for any promotion-enabled pipeline
# with N environments against a promotion-capable target, the composed plan has
# exactly one build+package for the artifact and N deploy steps referencing the
# SAME artifact reference, ordered per the stage graph; no environment re-runs
# build/package.
# Validates: Requirements R4.1, R4.2 (Plan B §11.6).
@pytest.mark.parametrize("target_platform", _CAPABLE_TARGETS)
@pytest.mark.parametrize("n_envs", _PATH_LENGTHS)
def test_property9_builds_once_and_deploys_same_reference(
    target_platform: str, n_envs: int
) -> None:
    """One build+package; N deploy+verify; all deploys share one reference."""
    pipeline = _promotion_ir(target_platform, n_envs)
    target_plugin = load_target_platform_plugin(target_platform)

    plan = compose_promotion_plan(pipeline, target_plugin)

    # exactly ONE build step, running build then package (in that order)
    assert plan.build_step.environment is None
    assert plan.build_step.verbs == PROMOTION_BUILD_VERBS == (
        StageVerb.BUILD,
        StageVerb.PACKAGE,
    )
    assert [v for v, _ in plan.build_step.results] == [StageVerb.BUILD, StageVerb.PACKAGE]

    # exactly N deploy steps, one per promote-path environment IN ORDER
    assert len(plan.deploy_steps) == n_envs
    assert [s.environment for s in plan.deploy_steps] == _env_names(n_envs)

    # every deploy step runs ONLY deploy+verify — never build/package (no rebuild)
    for step in plan.deploy_steps:
        assert step.verbs == PROMOTION_DEPLOY_VERBS == (StageVerb.DEPLOY, StageVerb.VERIFY)
        ran = [v for v, _ in step.results]
        assert ran == [StageVerb.DEPLOY, StageVerb.VERIFY]
        assert StageVerb.BUILD not in ran
        assert StageVerb.PACKAGE not in ran

    # all N deploys reference the SAME artifact reference
    refs = plan.deploy_references()
    assert len(refs) == n_envs
    assert len(set(refs)) == 1
    assert refs[0] == plan.artifact_reference

    # the reference is by-reference only (a shell var), never a digest/key value (R6)
    assert plan.artifact_reference.startswith("$")


@pytest.mark.parametrize("target_platform", _CAPABLE_TARGETS)
@pytest.mark.parametrize("n_envs", _PATH_LENGTHS)
def test_property9_same_reference_through_dispatch(
    target_platform: str, n_envs: int
) -> None:
    """The same guarantee holds end-to-end through the full dispatch path."""
    pipeline = _promotion_ir(target_platform, n_envs)
    result = dispatch(pipeline)

    plan = result.promotion_plan
    assert plan is not None, "dispatch must attach a promotion plan when enabled"
    assert [v for v, _ in plan.build_step.results] == [StageVerb.BUILD, StageVerb.PACKAGE]
    assert len(plan.deploy_steps) == n_envs
    assert len(set(plan.deploy_references())) == 1


@pytest.mark.parametrize("target_platform", _CAPABLE_TARGETS)
def test_promotion_reference_threaded_through_settings(target_platform: str) -> None:
    """The shared reference + logical name are threaded via VerbContext.settings (R6)."""
    pipeline = _promotion_ir(target_platform, 2)
    target_plugin = load_target_platform_plugin(target_platform)
    plan = compose_promotion_plan(pipeline, target_plugin, settings={"region": "r1"})

    # The reference is deterministic from the logical artifact name only.
    assert plan.artifact == "service-image"
    assert plan.artifact_reference == "$SERVICE_IMAGE_REF"

    # The package verb emits (exports) the reference; each deploy consumes it.
    package_cmds = "\n".join(
        c for v, res in plan.build_step.results if v is StageVerb.PACKAGE for c in res.commands
    )
    assert "SERVICE_IMAGE_REF" in package_cmds
    for step in plan.deploy_steps:
        deploy_cmds = "\n".join(
            c for v, res in step.results if v is StageVerb.DEPLOY for c in res.commands
        )
        assert "SERVICE_IMAGE_REF" in deploy_cmds


def test_promotion_settings_keys_are_stable() -> None:
    """The documented settings keys the plugins read are stable constants."""
    assert PROMOTION_ARTIFACT_REF_KEY == "promotion_artifact_ref"
    assert PROMOTION_ARTIFACT_NAME_KEY == "promotion_artifact"


# ===========================================================================
# 20.5 — fail-fast: promotion against a target lacking ARTIFACT_PROMOTION
# ===========================================================================
@pytest.mark.parametrize("target_platform", _INCAPABLE_TARGETS)
def test_promotion_against_incapable_target_fails_via_capability_gate(
    target_platform: str,
) -> None:
    """Requesting promotion against a non-capable target fails the 1b gate, not rebuilds.

    Through the full dispatch path the capability gate (§11.2) fires BEFORE any
    composition, naming the missing ``artifact_promotion`` capability — so the
    pipeline can never silently rebuild per environment.
    """
    envs = ["dev"]
    pipeline = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": target_platform,
            "project": {"name": "demo"},
            "environments": [{"name": e} for e in envs],
            "stages": [
                {"id": "build"},
                {"id": "deploy", "environment": "dev"},
            ],
            "cloud": {"provider": "azure", "auth": "oidc"},
            "promotion": {"enabled": True, "artifact": "svc", "path": envs},
        }
    )

    with pytest.raises(CapabilityError) as excinfo:
        dispatch(pipeline)

    message = str(excinfo.value)
    # names the missing capability and that the target axis must provide it
    assert Capability.ARTIFACT_PROMOTION.value in message
    assert "artifact promotion" in message or "promotion" in message


@pytest.mark.parametrize("target_platform", _INCAPABLE_TARGETS)
def test_compose_promotion_plan_directly_rejects_incapable_target(
    target_platform: str,
) -> None:
    """The composer itself also refuses an incapable target (defence in depth)."""
    envs = ["dev"]
    pipeline = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": target_platform,
            "project": {"name": "demo"},
            "environments": [{"name": e} for e in envs],
            "stages": [{"id": "build"}, {"id": "deploy", "environment": "dev"}],
            "cloud": {"provider": "azure", "auth": "oidc"},
            "promotion": {"enabled": True, "artifact": "svc", "path": envs},
        }
    )
    target_plugin = load_target_platform_plugin(target_platform)
    with pytest.raises(PromotionError) as excinfo:
        compose_promotion_plan(pipeline, target_plugin)
    assert Capability.ARTIFACT_PROMOTION.value in str(excinfo.value)


def test_capable_target_passes_the_gate_and_composes() -> None:
    """A satisfied promotion IR against a capable target dispatches + composes."""
    assert _CAPABLE_TARGETS, "expected at least one promotion-capable target on disk"
    target = _CAPABLE_TARGETS[0]
    pipeline = _promotion_ir(target, 2)
    result = dispatch(pipeline)  # no raise
    assert result.promotion_plan is not None


# ===========================================================================
# IR-level checks for the new promotion block
# ===========================================================================
def test_promotion_absent_is_backward_compatible() -> None:
    """When promotion is omitted the IR validates and dispatch attaches no plan."""
    pipeline = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "environments": [{"name": "dev"}],
            "stages": [{"id": "build"}, {"id": "deploy", "environment": "dev"}],
            "cloud": {"provider": "aws", "auth": "oidc"},
        }
    )
    assert pipeline.promotion is None
    result = dispatch(pipeline)
    assert result.promotion_plan is None


def test_promotion_path_arrow_string_is_normalised_to_a_list() -> None:
    """A 'dev -> staging -> prod' path is split and stored as an ordered list."""
    pipeline = _promotion_ir("aws-ecs", 3)
    # rebuild with the arrow-string form to exercise the before-validator
    doc = pipeline.model_dump()
    doc["promotion"]["path"] = "dev -> staging -> prod"
    reparsed = Pipeline.model_validate(doc)
    assert reparsed.promotion is not None
    assert reparsed.promotion.path == ["dev", "staging", "prod"]


def test_promotion_path_must_reference_declared_environments() -> None:
    """A promote-path entry that is not a declared environment fails fast."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        Pipeline.model_validate(
            {
                "version": 1,
                "ci_vendor": "github",
                "target_platform": "aws-ecs",
                "project": {"name": "demo"},
                "environments": [{"name": "dev"}],
                "stages": [{"id": "build"}, {"id": "deploy", "environment": "dev"}],
                "promotion": {"enabled": True, "artifact": "svc", "path": ["dev", "ghost"]},
            }
        )
    message = str(excinfo.value)
    assert "ghost" in message
    assert "not declared" in message or "not" in message


def test_promotion_path_rejects_duplicates() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Pipeline.model_validate(
            {
                "version": 1,
                "ci_vendor": "github",
                "target_platform": "aws-ecs",
                "project": {"name": "demo"},
                "environments": [{"name": "dev"}],
                "stages": [{"id": "build"}, {"id": "deploy", "environment": "dev"}],
                "promotion": {"enabled": True, "artifact": "svc", "path": ["dev", "dev"]},
            }
        )


def test_disabled_promotion_composes_nothing_in_dispatch() -> None:
    """promotion.enabled=false means no plan is composed (no ARTIFACT_PROMOTION needed)."""
    pipeline = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "databricks",  # NOT promotion-capable
            "project": {"name": "demo"},
            "environments": [{"name": "dev"}],
            "stages": [{"id": "build"}, {"id": "deploy", "environment": "dev"}],
            "cloud": {"provider": "azure", "auth": "oidc"},
            "promotion": {"enabled": False, "artifact": "svc", "path": ["dev"]},
        }
    )
    # disabled promotion does not require the capability, so dispatch succeeds and
    # attaches no plan.
    result = dispatch(pipeline)
    assert result.promotion_plan is None


# ===========================================================================
# Determinism — a promotion IR still generates byte-identically (R5.3)
# ===========================================================================
@pytest.mark.parametrize("target_platform", _CAPABLE_TARGETS)
def test_promotion_plan_is_deterministic_across_calls(target_platform: str) -> None:
    """Composing twice yields identical steps, references, and commands (R5.3)."""
    pipeline = _promotion_ir(target_platform, 3)
    target_plugin = load_target_platform_plugin(target_platform)

    first = compose_promotion_plan(pipeline, target_plugin)
    second = compose_promotion_plan(pipeline, target_plugin)

    assert first.artifact_reference == second.artifact_reference
    assert [s.environment for s in first.deploy_steps] == [
        s.environment for s in second.deploy_steps
    ]

    def _cmds(plan) -> list[str]:
        out: list[str] = []
        for step in plan.all_steps:
            for _v, res in step.results:
                out.extend(res.commands)
        return out

    assert _cmds(first) == _cmds(second)
