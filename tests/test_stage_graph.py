"""Tests for the canonical stage dependency graph — the 1c gate (task 18, §11.3).

The core formalises the stage dependency graph: it validates declared
``depends_on`` edges, detects cycles, and produces a deterministic topological
order (``cicdgen.core.stage_graph``). The logic is platform-blind (R3.4) — it
reads only stage ids + their ``depends_on`` edges, never a vendor/platform.

Covered here, following the project's parametrized-property convention (no
hypothesis dependency — inputs are enumerated/seeded deterministically):

    * Property 3 — cycle detection: a generated acyclic graph validates and has a
      topological order; a graph with an introduced back-edge fails naming a node
      on the cycle;
    * Property 4 — undeclared-dependency rejection: an edge targeting an unknown
      id fails naming that edge; a graph whose edges all reference declared ids
      passes;
    * Property 5 — topological order respects dependencies AND is a stable
      permutation: every ``u -> v`` edge places ``u`` before ``v``; with no
      ``depends_on`` the output equals declared order; repeated runs are
      byte-identical;
    * backward-compat unit tests — pipelines without ``depends_on`` are handed to
      the CI plugin in declared order, byte-identically (R5.3).

Stages are built through ``Pipeline.model_validate`` so the full IR path (the new
``depends_on`` field + schema mirror) is exercised, then the pure graph functions
are driven directly.
"""

from __future__ import annotations

import random

import pytest

from cicdgen.core.stage_graph import (
    CyclicDependencyError,
    UndeclaredDependencyError,
    build_ordered_stage_graph,
    detect_cycles,
    ordered_stages,
    topological_order,
    validate_dependencies,
)
from cicdgen.ir import Pipeline
from cicdgen.ir.models import Stage


# ---------------------------------------------------------------------------
# Builders — stages come through the real IR so depends_on is validated end-to-end
# ---------------------------------------------------------------------------
def _pipeline(stage_specs: list[dict]) -> Pipeline:
    """Build a validated IR from a list of ``{id, depends_on?}`` stage specs."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": stage_specs,
        }
    )


def _stages(stage_specs: list[dict]) -> tuple[Stage, ...]:
    return tuple(_pipeline(stage_specs).stages)


def _order_ids(stages: tuple[Stage, ...]) -> list[str]:
    return [s.id for s in stages]


# ---------------------------------------------------------------------------
# Deterministic random DAG generator (seeded; no hypothesis)
# ---------------------------------------------------------------------------
def _random_dag_specs(rng: random.Random, n: int) -> list[dict]:
    """Generate specs for an n-node DAG.

    Nodes are named ``s0..s{n-1}`` and shuffled into a random declared order;
    each edge only points from a LOWER to a HIGHER topological rank, so the graph
    is acyclic by construction regardless of the declared (array) order.
    """
    ranks = list(range(n))
    labels = [f"s{i}" for i in ranks]
    # topological rank of each label = its index; edges go rank(dep) < rank(node).
    specs_by_label: dict[str, dict] = {}
    for i, label in enumerate(labels):
        possible_deps = labels[:i]
        k = rng.randint(0, len(possible_deps))
        deps = rng.sample(possible_deps, k) if k else []
        spec: dict = {"id": label}
        if deps:
            spec["depends_on"] = deps
        specs_by_label[label] = spec
    # Declared (array) order is a random permutation — ordering must NOT depend
    # on declaration order beyond the deterministic tie-break.
    declared = labels[:]
    rng.shuffle(declared)
    return [specs_by_label[label] for label in declared]


_RANDOM_DAG_SEEDS = list(range(40))


# ===========================================================================
# Property 4 — stage dependencies must be declared
# ===========================================================================
# Feature: agentic-cicd-generator, Property 4: Stage dependencies must be declared
# — validate_dependencies fails naming an edge whose depends_on id names no
# declared stage, and passes when every edge references a declared id.
# Validates: Requirements R1.1, R1.2 (Plan B §11.3).
@pytest.mark.parametrize("seed", _RANDOM_DAG_SEEDS)
def test_property4_all_declared_edges_pass(seed: int) -> None:
    """For any generated DAG whose edges all reference declared ids, validation passes."""
    rng = random.Random(seed)
    n = rng.randint(1, 8)
    stages = _stages(_random_dag_specs(rng, n))
    # should not raise
    validate_dependencies(stages)


@pytest.mark.parametrize("seed", _RANDOM_DAG_SEEDS)
def test_property4_undeclared_edge_is_rejected_naming_the_edge(seed: int) -> None:
    """For any graph with an edge to an unknown id, validation fails naming that edge."""
    rng = random.Random(seed + 1000)
    n = rng.randint(1, 8)
    specs = _random_dag_specs(rng, n)
    # Inject an edge to an id that is guaranteed not to be declared.
    missing = "ghost-stage"
    victim = rng.choice(specs)
    victim["depends_on"] = [*victim.get("depends_on", []), missing]
    stages = _stages(specs)

    with pytest.raises(UndeclaredDependencyError) as excinfo:
        validate_dependencies(stages)

    message = str(excinfo.value)
    # names the offending (stage, missing-dep) edge
    assert victim["id"] in message
    assert missing in message


def test_undeclared_dependency_message_mirrors_cross_field_style() -> None:
    """The message mirrors stage_environment_must_be_declared's precise shape."""
    stages = _stages([{"id": "build", "depends_on": ["nope"]}, {"id": "deploy"}])
    with pytest.raises(UndeclaredDependencyError) as excinfo:
        validate_dependencies(stages)
    msg = str(excinfo.value)
    assert "stage 'build' depends_on 'nope'" in msg
    assert "not a declared stage" in msg
    # declared stages listed for context
    assert "build" in msg and "deploy" in msg


# ===========================================================================
# Property 3 — stage-graph cycle detection
# ===========================================================================
# Feature: agentic-cicd-generator, Property 3: Stage-graph cycle detection — a
# generated acyclic graph validates and has a topological order; a graph with an
# introduced back-edge fails naming a node on the cycle.
# Validates: Requirements R1.1, R3.4 (Plan B §11.3).
@pytest.mark.parametrize("seed", _RANDOM_DAG_SEEDS)
def test_property3_acyclic_graph_passes_and_has_topo_order(seed: int) -> None:
    """For any generated acyclic graph, detect_cycles passes and a topo order exists."""
    rng = random.Random(seed)
    n = rng.randint(1, 8)
    stages = _stages(_random_dag_specs(rng, n))

    detect_cycles(stages)  # no raise
    order = topological_order(stages)
    # a topological order exists and is a permutation of the input
    assert sorted(_order_ids(order)) == sorted(_order_ids(stages))


@pytest.mark.parametrize("seed", _RANDOM_DAG_SEEDS)
def test_property3_introduced_back_edge_fails_naming_a_cycle_node(seed: int) -> None:
    """For any DAG, adding a back-edge makes detect_cycles fail naming a cycle node."""
    rng = random.Random(seed + 2000)
    n = rng.randint(2, 8)
    specs = _random_dag_specs(rng, n)
    # Find a real dependency edge dep -> node (node depends_on dep) and add the
    # reverse edge dep depends_on node, guaranteeing at least a 2-cycle.
    by_id = {s["id"]: s for s in specs}
    candidates = [
        (s["id"], dep) for s in specs for dep in s.get("depends_on", [])
    ]
    if not candidates:
        # No edges to reverse (all independent) — force a self-dependency cycle.
        victim = rng.choice(specs)
        victim["depends_on"] = [*victim.get("depends_on", []), victim["id"]]
        cycle_nodes = {victim["id"]}
    else:
        node, dep = rng.choice(candidates)
        by_id[dep]["depends_on"] = [*by_id[dep].get("depends_on", []), node]
        cycle_nodes = {node, dep}

    stages = _stages(specs)
    with pytest.raises(CyclicDependencyError) as excinfo:
        detect_cycles(stages)
    message = str(excinfo.value)
    # names at least one node that is genuinely on the cycle, as a path
    assert "->" in message
    assert any(node in message for node in cycle_nodes)


def test_self_dependency_is_a_cycle() -> None:
    stages = _stages([{"id": "a", "depends_on": ["a"]}])
    with pytest.raises(CyclicDependencyError) as excinfo:
        detect_cycles(stages)
    assert "a -> a" in str(excinfo.value)


def test_three_node_cycle_is_reported_as_a_path() -> None:
    stages = _stages(
        [
            {"id": "a", "depends_on": ["c"]},
            {"id": "b", "depends_on": ["a"]},
            {"id": "c", "depends_on": ["b"]},
        ]
    )
    with pytest.raises(CyclicDependencyError) as excinfo:
        detect_cycles(stages)
    msg = str(excinfo.value)
    assert "->" in msg
    # the three nodes each appear on the reported cycle
    for node in ("a", "b", "c"):
        assert node in msg


# ===========================================================================
# Property 5 — topological order respects deps AND is a stable permutation
# ===========================================================================
# Feature: agentic-cicd-generator, Property 5: Topological order respects
# dependencies and is a stable permutation — every u -> v edge places u before v;
# with no depends_on the output equals declared order; repeated runs are
# byte-identical.
# Validates: Requirements R4.1, R5.3 (Plan B §11.3).
@pytest.mark.parametrize("seed", _RANDOM_DAG_SEEDS)
def test_property5_order_respects_every_dependency(seed: int) -> None:
    """For any valid DAG, each depends_on edge u -> v places u before v."""
    rng = random.Random(seed)
    n = rng.randint(1, 8)
    stages = _stages(_random_dag_specs(rng, n))
    order = topological_order(stages)
    pos = {s.id: i for i, s in enumerate(order)}

    # permutation of the input
    assert sorted(pos) == sorted(s.id for s in stages)
    # every dependency precedes its dependent
    for stage in stages:
        for dep in stage.depends_on:
            assert pos[dep] < pos[stage.id], (
                f"dependency '{dep}' must precede dependent '{stage.id}'"
            )


@pytest.mark.parametrize("seed", _RANDOM_DAG_SEEDS)
def test_property5_repeated_runs_are_byte_identical(seed: int) -> None:
    """For any valid DAG, topological_order is deterministic across repeated runs."""
    rng = random.Random(seed)
    n = rng.randint(1, 8)
    specs = _random_dag_specs(rng, n)
    stages = _stages(specs)

    first = _order_ids(topological_order(stages))
    for _ in range(5):
        assert _order_ids(topological_order(stages)) == first


@pytest.mark.parametrize("seed", _RANDOM_DAG_SEEDS)
def test_property5_deterministic_tiebreak_is_earliest_declared(seed: int) -> None:
    """Among ready nodes the earliest-declared is always chosen (stable tie-break)."""
    rng = random.Random(seed + 500)
    n = rng.randint(1, 8)
    specs = _random_dag_specs(rng, n)
    stages = _stages(specs)
    declared_index = {s.id: i for i, s in enumerate(stages)}
    order = topological_order(stages)

    # Reconstruct the greedy choice: at each step, the chosen node must be the
    # earliest-declared among those whose deps are all already emitted.
    emitted: set[str] = set()
    by_id = {s.id: s for s in stages}
    for chosen in order:
        ready = [
            sid
            for sid in by_id
            if sid not in emitted
            and all(d in emitted for d in by_id[sid].depends_on)
        ]
        earliest = min(ready, key=lambda sid: declared_index[sid])
        assert chosen.id == earliest
        emitted.add(chosen.id)


@pytest.mark.parametrize("seed", _RANDOM_DAG_SEEDS)
def test_property5_no_depends_on_equals_declared_order(seed: int) -> None:
    """With no depends_on anywhere, the output equals the declared order exactly."""
    rng = random.Random(seed)
    n = rng.randint(1, 10)
    labels = [f"s{i}" for i in range(n)]
    rng.shuffle(labels)
    specs = [{"id": label} for label in labels]
    stages = _stages(specs)

    order = topological_order(stages)
    assert _order_ids(order) == _order_ids(stages) == labels


def test_known_dag_orders_as_expected() -> None:
    """A concrete diamond-ish DAG yields the expected deterministic order."""
    # lint -> test -> build -> deploy; security -> build. Declared out of order to
    # prove the tie-break uses declared order, not insertion order of edges.
    specs = [
        {"id": "build", "depends_on": ["test", "security"]},
        {"id": "security", "depends_on": ["lint"]},
        {"id": "deploy", "depends_on": ["build"]},
        {"id": "test", "depends_on": ["lint"]},
        {"id": "lint"},
    ]
    stages = _stages(specs)
    order = _order_ids(topological_order(stages))

    pos = {sid: i for i, sid in enumerate(order)}
    # dependency constraints hold
    assert pos["lint"] < pos["test"] < pos["build"] < pos["deploy"]
    assert pos["lint"] < pos["security"] < pos["build"]
    # deterministic: lint first (only ready node), then among {security,test}
    # both ready after lint -> earliest declared is 'security' (index 1) < 'test'
    # (index 3).
    assert order[0] == "lint"
    assert order[1] == "security"
    assert order[2] == "test"


# ===========================================================================
# Backward compatibility — pipelines without depends_on are unchanged (R5.3)
# ===========================================================================
def test_ordered_stages_without_depends_on_equals_declared() -> None:
    specs = [{"id": "test"}, {"id": "build"}, {"id": "deploy"}]
    stages = _stages(specs)
    assert _order_ids(ordered_stages(stages)) == ["test", "build", "deploy"]


def test_build_ordered_stage_graph_without_depends_on_preserves_declared_order() -> None:
    """A pipeline that does not use depends_on is handed to the CI plugin unchanged."""
    pipeline = _pipeline([{"id": "test"}, {"id": "build"}, {"id": "deploy"}])
    graph = build_ordered_stage_graph(pipeline)
    assert graph.pipeline is pipeline
    assert [s.id for s in graph.stages] == ["test", "build", "deploy"]
    # same Stage objects as the IR declared them (no copying / reordering)
    assert list(graph.stages) == list(pipeline.stages)


def test_build_ordered_stage_graph_with_depends_on_reorders() -> None:
    """A pipeline that uses depends_on is handed the topologically ordered stages."""
    pipeline = _pipeline(
        [
            {"id": "deploy", "depends_on": ["build"]},
            {"id": "build", "depends_on": ["test"]},
            {"id": "test"},
        ]
    )
    graph = build_ordered_stage_graph(pipeline)
    assert [s.id for s in graph.stages] == ["test", "build", "deploy"]


def test_build_ordered_stage_graph_fails_fast_on_undeclared_dependency() -> None:
    pipeline = _pipeline([{"id": "build", "depends_on": ["missing"]}])
    with pytest.raises(UndeclaredDependencyError):
        build_ordered_stage_graph(pipeline)


def test_build_ordered_stage_graph_fails_fast_on_cycle() -> None:
    pipeline = _pipeline(
        [
            {"id": "a", "depends_on": ["b"]},
            {"id": "b", "depends_on": ["a"]},
        ]
    )
    with pytest.raises(CyclicDependencyError):
        build_ordered_stage_graph(pipeline)


# ===========================================================================
# IR-level field checks — the new depends_on field + its per-stage rules
# ===========================================================================
def test_depends_on_defaults_to_empty_list() -> None:
    stage = _pipeline([{"id": "solo"}]).stages[0]
    assert stage.depends_on == []


def test_depends_on_duplicate_ids_rejected_at_ir_level() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _pipeline([{"id": "a"}, {"id": "b", "depends_on": ["a", "a"]}])
