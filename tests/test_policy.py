"""Tests for the org-level policy engine — the 1d gate (task 19, §11.5).

The core enforces declarative org rules on the vendor-neutral IR BEFORE it
renders anything (fail-fast, §11.0 gate 1d). Each rule is a pure predicate; the
registry (``cicdgen.core.policy.RULES``) is a data-only ``dict`` so adding a rule
never adds a vendor/platform branch (R3.4). ``evaluate_policies`` aggregates EVERY
violation; ``enforce_policies`` raises a single ``PolicyError`` listing them all.

Covered here, following the project's parametrized-property convention (no
hypothesis dependency — inputs are enumerated/seeded deterministically):

    * Property 7 — policy evaluation is exact and vendor-neutral: the SAME policy
      set yields IDENTICAL violations for ``github × aws-ecs`` and
      ``azure-devops × databricks`` given the same IR shape, and a compliant IR
      yields zero violations; each violation names its rule + offending ir_path;
    * Property 8 — the temporal rule over an INJECTED day: a violation is reported
      IFF the injected day is the blocked day AND the IR has a prod-environment
      deploy stage; the wall clock is never read (deterministic, R5.3);
    * unit tests — each rule pass/fail, the document-shape validation, and the
      all-violations-at-once aggregation + the 1d gate wiring in the core.
"""

from __future__ import annotations

import random

import pytest

from cicdgen.core.policy import (
    POLICY_RELPATH,
    RULES,
    PolicyDocumentError,
    PolicyError,
    Violation,
    block_prod_deploy_friday,
    enforce_policies,
    evaluate_policies,
    load_policy_doc,
    minimum_coverage,
    require_security_scan,
)
from cicdgen.ir import Pipeline

# The two axis pairs Property 7 proves policy evaluation is identical across.
# They live together here, as DATA, only to drive the test — the engine itself
# never sees them (it reads the vendor-neutral IR).
_AXIS_PAIRS = (
    ("github", "aws-ecs"),
    ("azure-devops", "databricks"),
)

_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


# ---------------------------------------------------------------------------
# IR builder — structure is parameterised; the two axes are injected last so the
# SAME structural IR can be instantiated for each (ci_vendor, target_platform).
# ---------------------------------------------------------------------------
def _ir(
    *,
    ci_vendor: str = "github",
    target_platform: str = "aws-ecs",
    scans: tuple[str, ...] | None = None,
    coverage: float | None = None,
    prod_env: bool = False,
    prod_stage_enabled: bool = True,
) -> Pipeline:
    """Build a structurally-identical IR for any axis pair.

    ``scans`` go on the (single) build stage; ``coverage`` sets
    ``thresholds.coverage_min_percent``; ``prod_env`` declares a ``prod``
    environment plus a deploy stage targeting it (optionally disabled) so the
    temporal rule has a signal to find.
    """
    build_stage: dict = {"id": "build"}
    if scans is not None:
        build_stage["scans"] = list(scans)

    stages: list[dict] = [build_stage]
    environments: list[dict] = []
    if prod_env:
        environments.append({"name": "prod"})
        stages.append(
            {
                "id": "deploy-prod",
                "environment": "prod",
                "enabled": prod_stage_enabled,
            }
        )

    doc: dict = {
        "version": 1,
        "ci_vendor": ci_vendor,
        "target_platform": target_platform,
        "project": {"name": "demo"},
        "environments": environments,
        "stages": stages,
    }
    if coverage is not None:
        doc["thresholds"] = {"coverage_min_percent": coverage}
    return Pipeline.model_validate(doc)


def _policy_doc(
    *,
    scans: list[str] | None = None,
    percent: float | None = None,
    friday_envs: list[str] | None = None,
    day: str | None = None,
) -> dict:
    """Assemble a policies.yaml-shaped document from the requested rules."""
    policies: list[dict] = []
    if scans is not None:
        policies.append({"rule": "require_security_scan", "params": {"scans": scans}})
    if percent is not None:
        policies.append({"rule": "minimum_coverage", "params": {"percent": percent}})
    if friday_envs is not None:
        params: dict = {"environments": friday_envs}
        if day is not None:
            params["day"] = day
        policies.append({"rule": "block_prod_deploy_friday", "params": params})
    return {"version": 1, "policies": policies}


def _violation_keys(violations: list[Violation]) -> list[tuple[str, str]]:
    """Reduce violations to (rule, ir_path) pairs — the vendor-neutral identity."""
    return sorted((v.rule, v.ir_path) for v in violations)


# ===========================================================================
# Property 7 — policy evaluation is exact and vendor-neutral
# ===========================================================================
# Feature: agentic-cicd-generator, Property 7: Policy evaluation is exact and
# vendor-neutral — the same policy set yields identical violations for
# github×aws-ecs and azure-devops×databricks given the same IR shape; a compliant
# IR yields zero violations; each violation names its rule + offending ir_path.
# Validates: Requirements R2.1, R3.4 (Plan B §11.5).
_PROPERTY7_SEEDS = list(range(40))


def _random_case(rng: random.Random) -> tuple[dict, dict]:
    """A random (ir_kwargs, policy_doc) case — structure only, no axis bias."""
    want_scan_rule = rng.random() < 0.7
    want_cov_rule = rng.random() < 0.7
    want_friday_rule = rng.random() < 0.7

    # IR structure knobs.
    declared_scans = rng.sample(["sast", "secrets", "dependency"], rng.randint(0, 3))
    coverage = rng.choice([None, 0.0, 50.0, 79.0, 80.0, 95.0, 100.0])
    prod_env = rng.random() < 0.6
    prod_enabled = rng.random() < 0.8

    ir_kwargs = dict(
        scans=tuple(declared_scans) if declared_scans else (),
        coverage=coverage,
        prod_env=prod_env,
        prod_stage_enabled=prod_enabled,
    )

    policy_scans = (
        rng.sample(["sast", "secrets", "dependency"], rng.randint(1, 3))
        if want_scan_rule
        else None
    )
    policy_percent = rng.choice([50.0, 80.0, 90.0]) if want_cov_rule else None
    policy_friday = ["prod"] if want_friday_rule else None
    policy_doc = _policy_doc(
        scans=policy_scans, percent=policy_percent, friday_envs=policy_friday
    )
    return ir_kwargs, policy_doc


@pytest.mark.parametrize("seed", _PROPERTY7_SEEDS)
@pytest.mark.parametrize("clock_day", [None, "friday", "monday"])
def test_property7_identical_violations_across_axis_pairs(
    seed: int, clock_day: str | None
) -> None:
    """For any IR shape + policy set, the two axis pairs yield IDENTICAL violations."""
    rng = random.Random(seed)
    ir_kwargs, policy_doc = _random_case(rng)

    results: list[list[tuple[str, str]]] = []
    messages: list[list[str]] = []
    for ci_vendor, target_platform in _AXIS_PAIRS:
        pipeline = _ir(
            ci_vendor=ci_vendor, target_platform=target_platform, **ir_kwargs
        )
        violations = evaluate_policies(pipeline, policy_doc, clock_day=clock_day)
        results.append(_violation_keys(violations))
        messages.append(sorted(v.message for v in violations))

    # Vendor-neutral: both axis pairs produce exactly the same (rule, ir_path)
    # set AND the same messages — the engine read only the vendor-neutral IR.
    assert results[0] == results[1]
    assert messages[0] == messages[1]


@pytest.mark.parametrize("seed", _PROPERTY7_SEEDS)
def test_property7_exactness_matches_oracle(seed: int) -> None:
    """For any case, evaluate_policies returns EXACTLY the violations the IR breaks."""
    rng = random.Random(seed)
    ir_kwargs, policy_doc = _random_case(rng)
    clock_day = rng.choice([None, *_WEEKDAYS])
    pipeline = _ir(**ir_kwargs)

    got = set(_violation_keys(evaluate_policies(pipeline, policy_doc, clock_day=clock_day)))

    # Independent oracle: compute the expected broken rules directly.
    expected: set[tuple[str, str]] = set()
    for entry in policy_doc["policies"]:
        rule = entry["rule"]
        params = entry["params"]
        if rule == "require_security_scan":
            required = set(params["scans"])
            provided = set(ir_kwargs["scans"])
            if not required <= provided:
                expected.add((rule, "stages[].scans"))
        elif rule == "minimum_coverage":
            cov = ir_kwargs["coverage"]
            if cov is None or cov < params["percent"]:
                expected.add((rule, "thresholds.coverage_min_percent"))
        elif rule == "block_prod_deploy_friday":
            is_blocked_day = clock_day == "friday"
            has_prod_deploy = ir_kwargs["prod_env"] and ir_kwargs["prod_stage_enabled"]
            if is_blocked_day and has_prod_deploy:
                expected.add((rule, "stages[].environment"))

    assert got == expected


def test_property7_compliant_ir_yields_zero_violations() -> None:
    """A fully-compliant IR yields an empty violation list for both axis pairs."""
    policy_doc = _policy_doc(
        scans=["sast", "secrets"], percent=80.0, friday_envs=["prod"]
    )
    for ci_vendor, target_platform in _AXIS_PAIRS:
        pipeline = _ir(
            ci_vendor=ci_vendor,
            target_platform=target_platform,
            scans=("sast", "secrets", "dependency"),
            coverage=90.0,
            prod_env=True,
        )
        # Thursday — not the blocked day, so the temporal rule passes too.
        assert evaluate_policies(pipeline, policy_doc, clock_day="thursday") == []


def test_property7_every_violation_names_rule_and_path() -> None:
    """Each violation names its rule and a concrete ir_path (value-free)."""
    policy_doc = _policy_doc(scans=["sast"], percent=80.0, friday_envs=["prod"])
    pipeline = _ir(scans=(), coverage=10.0, prod_env=True)
    violations = evaluate_policies(pipeline, policy_doc, clock_day="friday")
    assert {v.rule for v in violations} == {
        "require_security_scan",
        "minimum_coverage",
        "block_prod_deploy_friday",
    }
    for v in violations:
        assert v.rule in RULES
        assert v.ir_path  # non-empty, points at the offending location


# ===========================================================================
# Property 8 — temporal prod-deploy policy over injected day
# ===========================================================================
# Feature: agentic-cicd-generator, Property 8: Temporal prod-deploy policy over
# injected day — block_prod_deploy_friday reports a violation IFF the injected
# day is the blocked day AND the IR contains a prod-environment deploy stage; the
# day is injected, never read from the wall clock (deterministic, R5.3).
# Validates: Requirements R2.1, R5.3 (Plan B §11.5).
@pytest.mark.parametrize("clock_day", [None, *_WEEKDAYS])
@pytest.mark.parametrize("has_prod_deploy", [True, False])
@pytest.mark.parametrize("blocked_day", ["friday", "monday"])
def test_property8_iff_blocked_day_and_prod_deploy(
    clock_day: str | None, has_prod_deploy: bool, blocked_day: str
) -> None:
    """A violation occurs IFF injected day == blocked day AND a prod deploy exists."""
    pipeline = _ir(prod_env=has_prod_deploy)
    params = {"environments": ["prod"], "day": blocked_day}
    violations = block_prod_deploy_friday(pipeline, params, clock_day=clock_day)

    should_fire = (clock_day == blocked_day) and has_prod_deploy
    assert bool(violations) == should_fire
    if should_fire:
        assert violations[0].rule == "block_prod_deploy_friday"
        assert violations[0].ir_path == "stages[].environment"


@pytest.mark.parametrize("clock_day", _WEEKDAYS)
def test_property8_deterministic_over_repeated_runs(clock_day: str) -> None:
    """Evaluation is deterministic for the same injected day across repeated runs."""
    pipeline = _ir(prod_env=True)
    params = {"environments": ["prod"]}  # default day == friday
    first = block_prod_deploy_friday(pipeline, params, clock_day=clock_day)
    for _ in range(5):
        assert block_prod_deploy_friday(pipeline, params, clock_day=clock_day) == first


def test_property8_no_injected_day_never_fires() -> None:
    """With clock_day=None the temporal rule cannot assert a violation (R5.3)."""
    pipeline = _ir(prod_env=True)
    assert block_prod_deploy_friday(pipeline, {"environments": ["prod"]}, clock_day=None) == []


def test_property8_disabled_prod_stage_is_not_a_deploy() -> None:
    """A disabled prod deploy stage does not count as a prod deploy."""
    pipeline = _ir(prod_env=True, prod_stage_enabled=False)
    violations = block_prod_deploy_friday(
        pipeline, {"environments": ["prod"]}, clock_day="friday"
    )
    assert violations == []


def test_property8_default_blocked_day_is_friday() -> None:
    """Omitting 'day' blocks on Friday by default."""
    pipeline = _ir(prod_env=True)
    assert block_prod_deploy_friday(pipeline, {"environments": ["prod"]}, clock_day="friday")
    assert block_prod_deploy_friday(pipeline, {"environments": ["prod"]}, clock_day="thursday") == []


def test_property8_day_match_is_case_insensitive() -> None:
    pipeline = _ir(prod_env=True)
    assert block_prod_deploy_friday(
        pipeline, {"environments": ["prod"], "day": "Friday"}, clock_day="FRIDAY"
    )


# ===========================================================================
# Unit tests — each rule pass/fail
# ===========================================================================
def test_require_security_scan_passes_when_union_covers_required() -> None:
    pipeline = _ir(scans=("sast", "secrets"))
    assert require_security_scan(pipeline, {"scans": ["sast"]}) == []
    assert require_security_scan(pipeline, {"scans": ["sast", "secrets"]}) == []


def test_require_security_scan_fails_naming_missing_scans() -> None:
    pipeline = _ir(scans=("sast",))
    violations = require_security_scan(pipeline, {"scans": ["sast", "secrets"]})
    assert len(violations) == 1
    v = violations[0]
    assert v.rule == "require_security_scan"
    assert v.ir_path == "stages[].scans"
    assert "secrets" in v.message
    assert "sast" not in v.message.split("are not declared")[0]


def test_require_security_scan_empty_required_is_noop() -> None:
    pipeline = _ir(scans=())
    assert require_security_scan(pipeline, {"scans": []}) == []


def test_require_security_scan_union_across_stages() -> None:
    """The required set is covered by the UNION of all stages' scans."""
    pipeline = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [
                {"id": "a", "scans": ["sast"]},
                {"id": "b", "scans": ["secrets"]},
            ],
        }
    )
    assert require_security_scan(pipeline, {"scans": ["sast", "secrets"]}) == []


def test_minimum_coverage_passes_at_or_above_floor() -> None:
    assert minimum_coverage(_ir(coverage=80.0), {"percent": 80}) == []
    assert minimum_coverage(_ir(coverage=95.0), {"percent": 80}) == []


def test_minimum_coverage_fails_below_floor() -> None:
    violations = minimum_coverage(_ir(coverage=50.0), {"percent": 80})
    assert len(violations) == 1
    assert violations[0].ir_path == "thresholds.coverage_min_percent"
    assert "80" in violations[0].message


def test_minimum_coverage_missing_threshold_is_a_violation() -> None:
    violations = minimum_coverage(_ir(coverage=None), {"percent": 80})
    assert len(violations) == 1
    assert "unset" in violations[0].message


# ===========================================================================
# Unit tests — aggregation (all violations at once) + enforce
# ===========================================================================
def test_evaluate_aggregates_all_violations_in_one_pass() -> None:
    """A broken IR reports EVERY violation, not just the first."""
    policy_doc = _policy_doc(scans=["sast", "secrets"], percent=80.0, friday_envs=["prod"])
    pipeline = _ir(scans=(), coverage=10.0, prod_env=True)
    violations = evaluate_policies(pipeline, policy_doc, clock_day="friday")
    assert len(violations) == 3
    assert {v.rule for v in violations} == set(RULES)


def test_enforce_raises_policy_error_listing_every_violation() -> None:
    policy_doc = _policy_doc(scans=["sast"], percent=80.0, friday_envs=["prod"])
    pipeline = _ir(scans=(), coverage=10.0, prod_env=True)
    with pytest.raises(PolicyError) as excinfo:
        enforce_policies(pipeline, policy_doc, clock_day="friday")
    err = excinfo.value
    assert len(err.violations) == 3
    message = str(err)
    for rule in RULES:
        assert rule in message
    assert "3 violation(s)" in message


def test_enforce_is_noop_on_compliant_ir() -> None:
    policy_doc = _policy_doc(scans=["sast"], percent=80.0, friday_envs=["prod"])
    pipeline = _ir(scans=("sast",), coverage=90.0, prod_env=True)
    enforce_policies(pipeline, policy_doc, clock_day="monday")  # no raise


def test_enforce_is_noop_when_doc_is_none() -> None:
    enforce_policies(_ir(scans=()), None, clock_day="friday")  # no raise


def test_evaluate_none_doc_returns_empty() -> None:
    assert evaluate_policies(_ir(scans=()), None) == []


def test_evaluate_runs_rules_in_document_order() -> None:
    """Violations are concatenated in document order (deterministic ordering)."""
    doc_a = _policy_doc(percent=80.0)  # coverage first
    doc_a["policies"].append({"rule": "require_security_scan", "params": {"scans": ["sast"]}})
    pipeline = _ir(scans=(), coverage=10.0)
    order = [v.rule for v in evaluate_policies(pipeline, doc_a)]
    assert order == ["minimum_coverage", "require_security_scan"]


# ===========================================================================
# Unit tests — document-shape validation (fail fast, precise)
# ===========================================================================
def test_unknown_rule_fails_fast_naming_it() -> None:
    doc = {"version": 1, "policies": [{"rule": "no_such_rule", "params": {}}]}
    with pytest.raises(PolicyDocumentError) as excinfo:
        evaluate_policies(_ir(scans=()), doc)
    assert "no_such_rule" in str(excinfo.value)


def test_bad_version_fails_fast() -> None:
    doc = {"version": 2, "policies": []}
    with pytest.raises(PolicyDocumentError):
        evaluate_policies(_ir(scans=()), doc)


def test_policies_must_be_a_list() -> None:
    doc = {"version": 1, "policies": {"rule": "minimum_coverage"}}
    with pytest.raises(PolicyDocumentError):
        evaluate_policies(_ir(scans=()), doc)


def test_unknown_scan_type_in_params_fails_fast() -> None:
    doc = _policy_doc(scans=["sast", "bogus"])
    with pytest.raises(PolicyDocumentError) as excinfo:
        evaluate_policies(_ir(scans=("sast",)), doc)
    assert "bogus" in str(excinfo.value)


def test_params_must_be_a_mapping() -> None:
    doc = {"version": 1, "policies": [{"rule": "minimum_coverage", "params": [80]}]}
    with pytest.raises(PolicyDocumentError):
        evaluate_policies(_ir(scans=()), doc)


# ===========================================================================
# Unit tests — the committed example + the policy file loader
# ===========================================================================
def test_load_policy_doc_absent_returns_none(tmp_path) -> None:
    assert load_policy_doc(tmp_path) is None


def test_load_policy_doc_reads_committed_shape(tmp_path) -> None:
    cicd = tmp_path / ".cicd"
    cicd.mkdir()
    (cicd / "policies.yaml").write_text(
        "version: 1\npolicies:\n  - rule: minimum_coverage\n    params: { percent: 80 }\n",
        encoding="utf-8",
    )
    doc = load_policy_doc(tmp_path)
    assert doc is not None
    assert doc["version"] == 1
    assert doc["policies"][0]["rule"] == "minimum_coverage"


def test_policy_relpath_is_not_a_local_ignored_file() -> None:
    """policies.yaml is committed (org-level), NOT a git-ignored *.local.* file."""
    assert POLICY_RELPATH == ".cicd/policies.yaml"
    assert ".local." not in POLICY_RELPATH


def test_registry_is_data_only_and_covers_the_three_rules() -> None:
    """The rule registry is a plain dict mapping the three documented rules."""
    assert set(RULES) == {
        "require_security_scan",
        "minimum_coverage",
        "block_prod_deploy_friday",
    }
    assert all(callable(p) for p in RULES.values())


# ===========================================================================
# 19.3 — the 1d gate is wired into the core (generate/plan) BEFORE render
# ===========================================================================
from cicdgen.core import generate, plan  # noqa: E402
from cicdgen.plugins import (  # noqa: E402
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


class _FakeCI(CIVendorPlugin):
    name = "github"

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        return (GeneratedFile(path=".github/workflows/ci.yml", content="ci\n"),)


class _FakeTarget(TargetPlatformPlugin):
    name = "aws-ecs"
    capabilities = _ALL_CAPS

    def _verb(self, ctx: VerbContext) -> VerbResult:
        return VerbResult(commands=(ctx.verb.value,))

    lint = _verb
    test = _verb
    build = _verb
    package = _verb
    deploy = _verb
    verify = _verb

    def supply_iac(self, pipeline: Pipeline) -> IaCArtifacts:
        return IaCArtifacts(summary="infra")


def _registry() -> dict:
    return {
        (PluginFamily.CI_VENDOR, "github"): _FakeCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): _FakeTarget(),
    }


def _write_policies(repo_root, body: str) -> None:
    cicd = repo_root / ".cicd"
    cicd.mkdir(parents=True, exist_ok=True)
    (cicd / "policies.yaml").write_text(body, encoding="utf-8")


def test_core_gate_no_policy_file_is_backward_compatible(tmp_path) -> None:
    """With no policies.yaml the 1d gate is a no-op: generation proceeds."""
    result = generate(_ir(scans=()), tmp_path, registry=_registry())
    assert ".github/workflows/ci.yml" in result.written


def test_core_gate_raises_policy_error_before_writing(tmp_path) -> None:
    """A broken policy fails generation and writes NO file (fail-fast, pre-render)."""
    _write_policies(
        tmp_path,
        "version: 1\npolicies:\n  - rule: minimum_coverage\n    params: { percent: 80 }\n",
    )
    with pytest.raises(PolicyError):
        generate(_ir(scans=(), coverage=10.0), tmp_path, registry=_registry())
    # Nothing was written: the gate precedes the write phase.
    assert not (tmp_path / ".github").exists()


def test_core_gate_passes_compliant_ir(tmp_path) -> None:
    _write_policies(
        tmp_path,
        "version: 1\npolicies:\n  - rule: minimum_coverage\n    params: { percent: 80 }\n",
    )
    result = generate(_ir(scans=(), coverage=90.0), tmp_path, registry=_registry())
    assert ".github/workflows/ci.yml" in result.written


def test_core_gate_temporal_default_is_noop_without_clock_day(tmp_path) -> None:
    """The temporal rule does NOT fire unless clock_day is injected (R5.3)."""
    _write_policies(
        tmp_path,
        "version: 1\npolicies:\n  - rule: block_prod_deploy_friday\n"
        "    params: { environments: [prod] }\n",
    )
    # No clock_day -> deterministic no-op even though a prod deploy exists.
    result = generate(_ir(prod_env=True), tmp_path, registry=_registry())
    assert ".github/workflows/ci.yml" in result.written


def test_core_gate_temporal_fires_with_injected_blocked_day(tmp_path) -> None:
    _write_policies(
        tmp_path,
        "version: 1\npolicies:\n  - rule: block_prod_deploy_friday\n"
        "    params: { environments: [prod] }\n",
    )
    with pytest.raises(PolicyError):
        plan(_ir(prod_env=True), tmp_path, registry=_registry(), clock_day="friday")
    # A non-blocked injected day passes.
    result = plan(_ir(prod_env=True), tmp_path, registry=_registry(), clock_day="tuesday")
    assert ".github/workflows/ci.yml" in result.paths
