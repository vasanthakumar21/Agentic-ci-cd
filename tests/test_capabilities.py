"""Tests for the plugin capability gate (task 17, design §11.2, the 1b gate).

The core lets each plugin DECLARE the capabilities it supports (data —
:class:`cicdgen.plugins.Capability`) and validates an IR against the UNION of the
two chosen plugins' declared sets, failing fast with a precise, value-free message
when the IR structure requires something neither axis provides.

Two pure functions make up the gate (``cicdgen.core.capabilities``):

    * ``required_capabilities(pipeline)`` — derived from IR STRUCTURE alone
      (never the plugin names, R3.4);
    * ``validate_capabilities(pipeline, ci_plugin, target_plugin)`` — succeeds
      iff ``required ⊆ (ci.capabilities | target.capabilities)``.

Covered here:
    * Property 2 — soundness + completeness: over every split of a capability set
      across the two axes and every IR signal combination, ``validate_capabilities``
      raises IFF ``required - provided`` is non-empty, and the message names a
      capability that is genuinely absent (never one that is present);
    * fail-fast unit tests — an IR requesting a capability neither axis provides
      fails with the precise message (missing capability + IR signal + axis), and
      a satisfied IR passes;
    * every real plugin pair on disk, enumerated DYNAMICALLY (no vendor/platform
      literals in the test), satisfies a representative IR.

Plugins are injected via tiny in-memory fakes whose declared ``capabilities`` are
tunable, so the gate is exercised as a pure function without the filesystem; the
real on-disk plugins are enumerated through ``AvailablePlugins`` so the test holds
as plugins are added or removed.
"""

from __future__ import annotations

from itertools import combinations

import pytest

from cicdgen.core.capabilities import (
    CapabilityError,
    required_capabilities,
    validate_capabilities,
)
from cicdgen.ir import Pipeline
from cicdgen.ir.plugins import AvailablePlugins
from cicdgen.plugins import (
    Capability,
    CIVendorPlugin,
    GeneratedFile,
    IaCArtifacts,
    ProviderPlugin,
    StageGraph,
    TargetPlatformPlugin,
    VerbContext,
    VerbResult,
    load_ci_vendor_plugin,
    load_target_platform_plugin,
)


# ---------------------------------------------------------------------------
# Tunable in-memory fakes — only the declared capability set matters here
# ---------------------------------------------------------------------------
def _ci(capabilities: frozenset[Capability]) -> CIVendorPlugin:
    class _CI(CIVendorPlugin):
        name = "fake-ci"

        def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
            return (GeneratedFile(path=".ci/pipeline.yml", content="x"),)

    _CI.capabilities = capabilities
    return _CI()


def _target(capabilities: frozenset[Capability]) -> TargetPlatformPlugin:
    class _T(TargetPlatformPlugin):
        name = "fake-target"

        def _v(self, ctx: VerbContext) -> VerbResult:
            return VerbResult(commands=(ctx.verb.value,))

        lint = _v
        test = _v
        build = _v
        package = _v
        deploy = _v
        verify = _v

        def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
            return IaCArtifacts(summary="infra")

    _T.capabilities = capabilities
    return _T()


# ---------------------------------------------------------------------------
# IR builders — each knob flips exactly one required capability (structure only)
# ---------------------------------------------------------------------------
def _ir(
    *,
    approvals: bool = False,
    manual_gate: bool = False,
    manual_stage: bool = False,
    oidc: bool = False,
    scans: bool = False,
    environments: bool = False,
) -> Pipeline:
    """Build an IR whose STRUCTURE triggers exactly the requested capabilities.

    Each flag maps to one IR signal from the §11.2 table, so the required set is
    predictable from the flags alone — nothing here references a plugin name.
    """
    stage: dict = {"id": "build"}
    if approvals:
        stage["approvals"] = {"required": 1}
    if manual_stage:
        stage["when"] = "manual"
    if scans:
        stage["scans"] = ["sast"]

    envs: list[dict] = []
    if environments or manual_gate:
        envs = [{"name": "dev", "gate": "manual" if manual_gate else "auto"}]
        if manual_gate:
            stage["environment"] = "dev"

    doc: dict = {
        "version": 1,
        "ci_vendor": "github",
        "target_platform": "aws-ecs",
        "project": {"name": "demo"},
        "environments": envs,
        "stages": [stage],
    }
    if oidc:
        doc["cloud"] = {"provider": "aws", "auth": "oidc"}
    return Pipeline.model_validate(doc)


def _expected_required(
    *,
    approvals: bool,
    manual_gate: bool,
    manual_stage: bool,
    oidc: bool,
    scans: bool,
    environments: bool,
) -> set[Capability]:
    req: set[Capability] = set()
    if approvals:
        req.add(Capability.APPROVALS)
    if manual_gate or manual_stage:
        req.add(Capability.MANUAL_GATES)
    if oidc:
        req.add(Capability.OIDC)
    if scans:
        req.add(Capability.SECURITY_SCANS)
    # a manual_gate implies an environment is declared, hence ENVIRONMENTS too
    if environments or manual_gate:
        req.add(Capability.ENVIRONMENTS)
    return req


# ===========================================================================
# required_capabilities — derivation is exactly the §11.2 table
# ===========================================================================
_FLAG_NAMES = ("approvals", "manual_gate", "manual_stage", "oidc", "scans", "environments")


def _all_flag_combos() -> list[dict[str, bool]]:
    combos: list[dict[str, bool]] = []
    for bits in range(2 ** len(_FLAG_NAMES)):
        flags = {
            name: bool(bits & (1 << i)) for i, name in enumerate(_FLAG_NAMES)
        }
        combos.append(flags)
    return combos


@pytest.mark.parametrize("flags", _all_flag_combos())
def test_required_matches_ir_structure(flags: dict[str, bool]) -> None:
    """``required_capabilities`` derives exactly the §11.2 table from structure."""
    pipeline = _ir(**flags)
    assert required_capabilities(pipeline) == _expected_required(**flags)


# ===========================================================================
# Property 2 — capability validation is sound and complete
# ===========================================================================
# Feature: agentic-cicd-generator, Property 2: Capability validation is sound and
# complete — validate_capabilities succeeds IFF every capability the IR requires
# (derived from structure) is in the UNION of the two plugins' declared sets; on
# failure the error names a capability that is genuinely absent.
# Validates: Requirements R3.4 (Plan B §11.2, §11.6).

# A representative IR that requires several capabilities at once; the required set
# is non-trivial so the split across axes genuinely exercises the union.
_RICH_FLAGS = {
    "approvals": True,
    "manual_gate": True,
    "manual_stage": False,
    "oidc": True,
    "scans": True,
    "environments": True,
}


def _subsets(items: list[Capability]) -> list[frozenset[Capability]]:
    out: list[frozenset[Capability]] = []
    for r in range(len(items) + 1):
        for combo in combinations(items, r):
            out.append(frozenset(combo))
    return out


@pytest.mark.parametrize("flags", _all_flag_combos())
def test_property2_sound_and_complete_over_axis_splits(flags: dict[str, bool]) -> None:
    """For every IR and every split of a capability pool across the two axes,
    validation succeeds IFF required ⊆ (ci ∪ target); failures name an absent cap.
    """
    pipeline = _ir(**flags)
    required = required_capabilities(pipeline)

    # Pool to split across the axes = the required caps plus one always-irrelevant
    # extra, so both "exactly enough" and "missing one" cases are generated.
    pool = sorted(required, key=lambda c: c.value)
    # Enumerate every way to give each pooled capability to the ci axis, the
    # target axis, or neither — that is 3^len(pool). Keep the pool small (<=6).
    def _assignments(caps: list[Capability]):
        if not caps:
            yield (frozenset(), frozenset())
            return
        head, *tail = caps
        for ci_rest, tgt_rest in _assignments(tail):
            yield (ci_rest | {head}, tgt_rest)          # head -> ci
            yield (ci_rest, tgt_rest | {head})          # head -> target
            yield (ci_rest, tgt_rest)                   # head -> neither

    for ci_caps, tgt_caps in _assignments(pool):
        ci_plugin = _ci(frozenset(ci_caps))
        target_plugin = _target(frozenset(tgt_caps))
        provided = ci_caps | tgt_caps
        missing = required - provided

        if missing:
            with pytest.raises(CapabilityError) as excinfo:
                validate_capabilities(pipeline, ci_plugin, target_plugin)
            message = str(excinfo.value)
            # Soundness: every capability named as missing is genuinely absent ...
            for cap in missing:
                assert cap.value in message
            # ... and no capability that IS provided is reported as missing.
            for cap in provided:
                # a provided cap may still appear in the "provided=[...]" list, so
                # assert it is not flagged on a "is required because" line.
                assert f"'{cap.value}' is required because" not in message
        else:
            # Completeness: a satisfied split never raises.
            validate_capabilities(pipeline, ci_plugin, target_plugin)


def test_property2_union_across_axes_is_what_counts() -> None:
    """OIDC supplied by EITHER axis satisfies the requirement (union, §11.2)."""
    pipeline = _ir(oidc=True, environments=True)

    # ci provides OIDC, target provides ENVIRONMENTS — neither alone is enough,
    # but the union is.
    ci_plugin = _ci(frozenset({Capability.OIDC}))
    target_plugin = _target(frozenset({Capability.ENVIRONMENTS}))
    validate_capabilities(pipeline, ci_plugin, target_plugin)  # no raise

    # target provides OIDC instead — still satisfied by the union.
    ci_plugin2 = _ci(frozenset())
    target_plugin2 = _target(frozenset({Capability.OIDC, Capability.ENVIRONMENTS}))
    validate_capabilities(pipeline, ci_plugin2, target_plugin2)  # no raise


# ===========================================================================
# 17.5 — fail-fast unit tests (precise message + satisfied IR passes)
# ===========================================================================
def test_missing_capability_fails_with_precise_message() -> None:
    """An IR requesting a capability neither axis provides fails precisely."""
    pipeline = _ir(approvals=True)  # requires APPROVALS
    ci_plugin = _ci(frozenset())    # provides nothing
    target_plugin = _target(frozenset())

    with pytest.raises(CapabilityError) as excinfo:
        validate_capabilities(pipeline, ci_plugin, target_plugin)

    message = str(excinfo.value)
    # names the missing capability ...
    assert Capability.APPROVALS.value in message
    # ... the IR signal that required it ...
    assert "approvals" in message
    # ... and which axis must provide it.
    assert "ci_vendor" in message
    # value-free: no secret-ish value, just names (R6).
    assert "fake-ci" in message and "fake-target" in message


def test_satisfied_ir_passes() -> None:
    """A satisfied IR returns None (no raise)."""
    pipeline = _ir(approvals=True, environments=True, oidc=True, scans=True)
    ci_plugin = _ci(
        frozenset(
            {Capability.APPROVALS, Capability.SECURITY_SCANS, Capability.OIDC}
        )
    )
    target_plugin = _target(frozenset({Capability.ENVIRONMENTS}))

    assert validate_capabilities(pipeline, ci_plugin, target_plugin) is None


def test_empty_capability_plugin_degrades_safely_on_bare_ir() -> None:
    """A plugin declaring no capabilities still passes an IR that requires none."""
    # No environments, no approvals, no scans, no oidc, no manual gates.
    pipeline = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "environments": [],
            "stages": [{"id": "build"}],
        }
    )
    assert required_capabilities(pipeline) == set()
    validate_capabilities(pipeline, _ci(frozenset()), _target(frozenset()))


def test_missing_message_lists_every_absent_capability() -> None:
    """When several capabilities are absent, each is named on its own line."""
    pipeline = _ir(approvals=True, scans=True, environments=True)
    ci_plugin = _ci(frozenset())
    target_plugin = _target(frozenset())

    with pytest.raises(CapabilityError) as excinfo:
        validate_capabilities(pipeline, ci_plugin, target_plugin)

    message = str(excinfo.value)
    for cap in (Capability.APPROVALS, Capability.SECURITY_SCANS, Capability.ENVIRONMENTS):
        assert f"'{cap.value}' is required because" in message


# ===========================================================================
# Every real plugin pair on disk satisfies a representative IR (dynamic)
# ===========================================================================
def test_all_real_plugin_pairs_satisfy_a_representative_ir() -> None:
    """Enumerate plugins DYNAMICALLY; every pair passes a representative IR.

    No vendor/platform literal lives in this test (R3.4): names come from the
    filesystem scan. The IR declares an environment + OIDC — capabilities every
    shipped CI/target pair is expected to cover between them.
    """
    available = AvailablePlugins.discover()
    assert available.ci, "expected at least one ci plugin on disk"
    assert available.target, "expected at least one target plugin on disk"

    for ci_name in sorted(available.ci):
        ci_plugin: ProviderPlugin = load_ci_vendor_plugin(ci_name)
        for target_name in sorted(available.target):
            target_plugin: ProviderPlugin = load_target_platform_plugin(target_name)
            pipeline = Pipeline.model_validate(
                {
                    "version": 1,
                    "ci_vendor": ci_name,
                    "target_platform": target_name,
                    "project": {"name": "demo"},
                    "environments": [{"name": "dev"}],
                    "stages": [{"id": "build", "environment": "dev"}],
                    "cloud": {"provider": "aws", "auth": "oidc"},
                }
            )
            # Should not raise: environments + oidc are covered by the union.
            validate_capabilities(pipeline, ci_plugin, target_plugin)
