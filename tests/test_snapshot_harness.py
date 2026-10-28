"""Snapshot conformance suite + render/conformance properties (task 21.2/21.3/21.4).

This module is the §11.8 harness in action. It walks EVERY target plugin
(discovered dynamically from the on-disk tree — no literal is load-bearing, R3.4)
crossed with EVERY committed fixture under ``tests/fixtures/ir/`` and:

    * **Conformance snapshots (21.2)** — asserts a fresh in-memory render equals the
      committed snapshot under ``tests/snapshots/<target>__<fixture>/``. Because the
      render is deterministic (R5.3), a mismatch means the plugin's output genuinely
      changed — a real regression signal, not flakiness. New §11.9 matrix plugins
      get coverage for free: drop a fixture, record the snapshot once, review it.

    * **Property 11 — snapshot render is deterministic (21.3)** — for every target ×
      fixture, rendering twice yields byte-identical output; and the committed
      snapshot equals a fresh render for an unchanged plugin.

    * **Property 12 — target-plugin family conformance (21.4)** — for every target
      plugin and every one of the six verbs, ``run_verb`` returns a ``VerbResult``
      whose files use relative POSIX paths that never escape the repo root and whose
      commands/files contain no literal secret VALUES (only ``$REF`` / ``${{ … }}``
      references); the six verbs are all exercisable; IaC is supplied; and the output
      is deterministic across two renders.

Following the project's parametrized-property convention (NO hypothesis dependency —
inputs are enumerated/seeded deterministically from the dynamic plugin set × the
committed fixtures), the same style as ``test_promotion.py`` / ``test_stage_graph.py``.

The snapshots are RECORDED once (``UPDATE_SNAPSHOTS=1``) and committed; thereafter
they are pure regression guards. To refresh after an intentional plugin change, run
the suite once with ``UPDATE_SNAPSHOTS=1`` and review the diff.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cicdgen.ir import AvailablePlugins, Pipeline
from cicdgen.plugins import StageVerb, load_target_platform_plugin
from cicdgen.plugins.base import VerbContext
from tests.harness.snapshot import (
    FIXTURES_DIR,
    assert_snapshot,
    cloud_provider_for_target,
    render_plugin,
)

# <repo>/tests/test_snapshot_harness.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[1]

# The six verbs every target plugin implements (design §8b / base.StageVerb).
SIX_VERBS: tuple[StageVerb, ...] = (
    StageVerb.LINT,
    StageVerb.TEST,
    StageVerb.BUILD,
    StageVerb.PACKAGE,
    StageVerb.DEPLOY,
    StageVerb.VERIFY,
)


# ---------------------------------------------------------------------------
# The conformance matrix: every target plugin × every committed fixture
# ---------------------------------------------------------------------------
def _all_target_platforms() -> list[str]:
    """Every target plugin discovered from the real on-disk tree (sorted, dynamic)."""
    return sorted(AvailablePlugins.discover(REPO_ROOT).target)


def _all_fixtures() -> list[str]:
    """Every committed fixture stem under tests/fixtures/ir/ (sorted)."""
    return sorted(p.stem for p in FIXTURES_DIR.glob("*.yaml"))


_TARGETS = _all_target_platforms()
_FIXTURES = _all_fixtures()


def _snapshot_name(target_platform: str, fixture: str) -> str:
    """Stable snapshot directory name for a (target, fixture) cell of the matrix."""
    return f"{target_platform}__{fixture}"


def _fixture_ir(fixture: str) -> Path:
    return FIXTURES_DIR / f"{fixture}.yaml"


# ---------------------------------------------------------------------------
# Secret-value leak detection (R6.3/R6.6) — mirrors test_plugin_samples_12_2.py
# ---------------------------------------------------------------------------
# An ACCOUNT-SPECIFIC IAM ARN embeds a 12-digit account id; the PUBLIC managed-policy
# form (arn:aws:iam::aws:policy/...) is NOT secret and is allowed.
_ACCOUNT_SPECIFIC_IAM_ARN = re.compile(r"arn:aws:iam::\d{12}:")
_AWS_ACCESS_KEY_ID = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
# An AWS secret access key: 40 chars of base64-ish. Guard against a literal value
# being embedded as `aws_secret_access_key = "....."` (reference-only is required).
_AWS_SECRET_KEY_ASSIGN = re.compile(
    r"(?i)aws_secret_access_key\s*[=:]\s*[\"']?[A-Za-z0-9/+]{40}[\"']?"
)


def _assert_no_literal_secret_values(content: str, where: str) -> None:
    """Assert a rendered artifact carries no literal secret VALUE (R6)."""
    assert not _ACCOUNT_SPECIFIC_IAM_ARN.search(content), (
        f"account-specific IAM ARN leaked into {where}"
    )
    assert not _AWS_ACCESS_KEY_ID.search(content), (
        f"literal AWS access-key id leaked into {where}"
    )
    assert not _AWS_SECRET_KEY_ASSIGN.search(content), (
        f"literal AWS secret access key leaked into {where}"
    )


def _is_safe_relative_posix_path(path: str) -> bool:
    """True iff ``path`` is a relative POSIX path that never escapes the repo root."""
    if not path:
        return False
    if path.startswith("/") or "\\" in path:
        return False
    # No drive letter, no parent-escaping segment.
    if re.match(r"^[A-Za-z]:", path):
        return False
    parts = path.split("/")
    return ".." not in parts


# ===========================================================================
# 21.2 — conformance snapshot suite: committed snapshot == fresh render
# ===========================================================================
@pytest.mark.parametrize("target_platform", _TARGETS)
@pytest.mark.parametrize("fixture", _FIXTURES)
def test_snapshot_conformance(target_platform: str, fixture: str) -> None:
    """Every target plugin × every fixture matches its committed snapshot (R5.3)."""
    rendered = render_plugin(target_platform, _fixture_ir(fixture))
    assert rendered, f"{target_platform}×{fixture}: render produced no files"
    assert_snapshot(_snapshot_name(target_platform, fixture), rendered)


def test_matrix_is_non_trivial() -> None:
    """Guard: the dynamic matrix actually covers the shipped plugins + fixtures."""
    # The four existing target plugins (task 8/9/12) must all be present.
    assert set(_TARGETS) >= {"aws-ecs", "databricks", "azure-functions", "kubernetes"}
    # Both representative fixtures (task 21.2) must be present.
    assert set(_FIXTURES) >= {"minimal", "full"}


# ===========================================================================
# Property 11 — snapshot render is deterministic (task 21.3)
# ===========================================================================
# Feature: agentic-cicd-generator, Property 11: Snapshot render is deterministic —
# for any fixture IR and any plugin, rendering twice yields byte-identical output;
# and a committed snapshot equals a fresh render for an unchanged plugin.
# Validates: Requirements R5.1, R5.3 (Plan B §11.8).
@pytest.mark.parametrize("target_platform", _TARGETS)
@pytest.mark.parametrize("fixture", _FIXTURES)
def test_property11_render_is_deterministic(target_platform: str, fixture: str) -> None:
    """Rendering the same fixture twice yields byte-identical output (R5.3)."""
    first = render_plugin(target_platform, _fixture_ir(fixture))
    second = render_plugin(target_platform, _fixture_ir(fixture))

    # Identical key set, identical byte content, identical ordering.
    assert list(first.keys()) == list(second.keys())
    assert first == second
    for path in first:
        assert first[path] == second[path], f"content drifted across renders: {path}"


@pytest.mark.parametrize("target_platform", _TARGETS)
@pytest.mark.parametrize("fixture", _FIXTURES)
def test_property11_committed_snapshot_equals_fresh_render(
    target_platform: str, fixture: str
) -> None:
    """The committed snapshot equals a fresh render for an unchanged plugin (R5.3)."""
    rendered = render_plugin(target_platform, _fixture_ir(fixture))
    # assert_snapshot (no update) raises on any drift, naming the path.
    assert_snapshot(_snapshot_name(target_platform, fixture), rendered)


# ===========================================================================
# Property 12 — target-plugin family conformance (task 21.4)
# ===========================================================================
# Feature: agentic-cicd-generator, Property 12: Target-plugin family conformance —
# for any target plugin and any of the six verbs, run_verb returns a VerbResult whose
# files use relative POSIX paths that never escape the repo root and whose
# commands/files contain no secret VALUES (only $REF / ${{ secrets.X }} references),
# and whose output is byte-identical across two renders.
# Validates: Requirements R3.3, R4.1, R5.3, R6.3, R6.6 (Plan B §11.9).
def _conformance_pipeline(target_platform: str) -> Pipeline:
    """A coherent IR for driving every verb of ``target_platform`` directly."""
    provider = cloud_provider_for_target(target_platform)
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": target_platform,
            "project": {"name": "conformance-svc", "language": "python"},
            "cloud": {"provider": provider, "region": "r1", "auth": "oidc"},
            "environments": [{"name": "dev", "gate": "auto"}],
            "stages": [
                {"id": "build"},
                {"id": "deploy-dev", "environment": "dev"},
            ],
        }
    )


@pytest.mark.parametrize("target_platform", _TARGETS)
def test_property12_every_verb_returns_safe_paths_and_no_secret_values(
    target_platform: str,
) -> None:
    """All six verbs: relative POSIX files, no literal secret values (R6.3/R6.6)."""
    plugin = load_target_platform_plugin(target_platform, plugins_root=REPO_ROOT / "plugins")
    pipeline = _conformance_pipeline(target_platform)
    build_stage = pipeline.stages[0]

    for verb in SIX_VERBS:
        ctx = VerbContext(
            pipeline=pipeline,
            stage=build_stage,
            verb=verb,
            settings={},
        )
        result = plugin.run_verb(ctx)

        for f in result.files:
            assert _is_safe_relative_posix_path(f.path), (
                f"{target_platform}.{verb}: unsafe path {f.path!r}"
            )
            _assert_no_literal_secret_values(
                f.content, f"{target_platform}.{verb} file {f.path}"
            )
        for cmd in result.commands:
            _assert_no_literal_secret_values(
                cmd, f"{target_platform}.{verb} command"
            )


@pytest.mark.parametrize("target_platform", _TARGETS)
def test_property12_all_six_verbs_are_exercisable(target_platform: str) -> None:
    """Every target plugin answers all six verbs uniformly (R4.1) without error."""
    plugin = load_target_platform_plugin(target_platform, plugins_root=REPO_ROOT / "plugins")
    pipeline = _conformance_pipeline(target_platform)
    stage = pipeline.stages[0]

    answered: list[StageVerb] = []
    for verb in SIX_VERBS:
        ctx = VerbContext(pipeline=pipeline, stage=stage, verb=verb, settings={})
        result = plugin.run_verb(ctx)  # uniform contract — never raises per verb
        # A verb may be a no-op on a platform (empty tuples), but the contract holds.
        assert isinstance(result.commands, tuple)
        assert isinstance(result.files, tuple)
        answered.append(verb)

    assert answered == list(SIX_VERBS)


@pytest.mark.parametrize("target_platform", _TARGETS)
def test_property12_target_supplies_iac(target_platform: str) -> None:
    """Every target plugin supplies backing IaC with safe, secret-free files (R4.2)."""
    plugin = load_target_platform_plugin(target_platform, plugins_root=REPO_ROOT / "plugins")
    pipeline = _conformance_pipeline(target_platform)

    iac = plugin.supply_iac(pipeline)
    assert iac.files, f"{target_platform}: supply_iac produced no files"
    for f in iac.files:
        assert _is_safe_relative_posix_path(f.path), (
            f"{target_platform} IaC: unsafe path {f.path!r}"
        )
        _assert_no_literal_secret_values(f.content, f"{target_platform} IaC file {f.path}")


@pytest.mark.parametrize("target_platform", _TARGETS)
@pytest.mark.parametrize("fixture", _FIXTURES)
def test_property12_render_output_is_deterministic_and_secret_free(
    target_platform: str, fixture: str
) -> None:
    """Family conformance over a full render: deterministic + secret-free (R5.3/R6)."""
    first = render_plugin(target_platform, _fixture_ir(fixture))
    second = render_plugin(target_platform, _fixture_ir(fixture))
    assert first == second  # byte-identical across renders

    for path, content in first.items():
        assert _is_safe_relative_posix_path(path), f"unsafe generated path {path!r}"
        _assert_no_literal_secret_values(content, f"rendered {path}")
