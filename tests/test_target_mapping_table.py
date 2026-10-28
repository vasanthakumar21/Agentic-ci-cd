"""Target mapping-table conformance tests (task 9.3, R3.3/R4.2/R7.1, design §8b).

Task 9.3 — *validate against the target mapping table (design §8b) in the isolated
workspace*. A live Databricks workspace run is not available locally (R9 isolation —
no workspace/credentials); the live isolated-workspace run is the manual runbook
captured in ``examples/client-azdo-databricks/iac/databricks/README.md``. Here we do
the mapping-table **CONFORMANCE** validation locally against the commands + IaC each
target plugin *generates*, using :mod:`cicdgen.validate.target_mapping`.

These tests load the REAL plugins (``plugins/target/aws-ecs`` and
``plugins/target/databricks``) via the loader — no mocks — and run
``assert_target_matches_mapping`` for BOTH against a representative IR, asserting:

    * each plugin's build/package/deploy/verify + IaC match its §8b column
      (aws-ecs = Docker/ECR/ECS shaped; databricks = wheel/DBFS/job shaped, NO image);
    * full §8b mapping-table coverage (every verb row + the IaC row represented for
      each platform); and
    * the cross-platform contrast — aws-ecs's anti-markers ARE databricks's shape and
      vice versa, so a plugin cannot satisfy both columns (the different-shape proof).

Secrets-by-reference (R6) and determinism (R5.3) are covered in
``tests/test_plugin_databricks.py`` / ``tests/test_plugin_aws_ecs.py``; this file is
focused on the §8b mapping-table conformance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cicdgen.ir import Pipeline
from cicdgen.plugins import load_target_platform_plugin
from cicdgen.validate import (
    IAC_ROW,
    MAPPED_VERBS,
    TARGET_MAPPING,
    TargetMappingValidationError,
    assert_cross_platform_contrast,
    assert_full_table_coverage,
    assert_target_matches_mapping,
    expected_rows,
)

# plugins/ live at <repo>/plugins; this file is <repo>/tests/test_target_mapping_table.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"


# ---------------------------------------------------------------------------
# Representative IRs — one per platform (same shape, different target_platform)
# ---------------------------------------------------------------------------
def _databricks_pipeline() -> Pipeline:
    """Azure DevOps × Databricks — notebooks/jobs, no image (the §8b databricks column)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "azure-devops",
            "target_platform": "databricks",
            "project": {"name": "demo-notebooks"},
            "cloud": {"provider": "aws", "region": "eu-west-2", "auth": "oidc"},
            "environments": [{"name": "prod", "gate": "manual"}],
            "stages": [
                {"id": "test", "runs": "pytest -q"},
                {"id": "build", "runs": "make build"},
                {"id": "deploy-prod", "runs": "make deploy", "environment": "prod"},
            ],
        }
    )


def _aws_ecs_pipeline() -> Pipeline:
    """GitHub × AWS ECS — Docker image to ECR/ECS (the §8b aws-ecs column)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo-service"},
            "cloud": {"provider": "aws", "region": "eu-west-2", "auth": "oidc"},
            "environments": [{"name": "prod", "gate": "manual"}],
            "stages": [
                {"id": "test", "runs": "make test"},
                {"id": "build", "runs": "make build"},
                {"id": "deploy-prod", "runs": "make deploy", "environment": "prod"},
            ],
        }
    )


def _db_plugin():
    return load_target_platform_plugin("databricks", plugins_root=PLUGINS_ROOT)


def _ecs_plugin():
    return load_target_platform_plugin("aws-ecs", plugins_root=PLUGINS_ROOT)


# ===========================================================================
# Per-platform conformance — each plugin matches its own §8b column
# ===========================================================================
def test_databricks_matches_its_mapping_column() -> None:
    report = assert_target_matches_mapping(_db_plugin(), _databricks_pipeline())
    assert report.target_platform == "databricks"


def test_aws_ecs_matches_its_mapping_column() -> None:
    report = assert_target_matches_mapping(_ecs_plugin(), _aws_ecs_pipeline())
    assert report.target_platform == "aws-ecs"


# ===========================================================================
# Full mapping-table coverage — EVERY §8b row represented for each platform
# ===========================================================================
def test_databricks_covers_every_mapping_row() -> None:
    report = assert_target_matches_mapping(_db_plugin(), _databricks_pipeline())
    assert_full_table_coverage(report)  # raises if any row skipped
    for row in expected_rows():
        assert report.covers(row), f"databricks did not cover §8b row {row!r}"


def test_aws_ecs_covers_every_mapping_row() -> None:
    report = assert_target_matches_mapping(_ecs_plugin(), _aws_ecs_pipeline())
    assert_full_table_coverage(report)
    for row in expected_rows():
        assert report.covers(row), f"aws-ecs did not cover §8b row {row!r}"


def test_expected_rows_are_the_four_verbs_plus_iac() -> None:
    rows = expected_rows()
    assert IAC_ROW in rows
    for verb in MAPPED_VERBS:
        assert str(verb) in rows
    # build / package / deploy / verify + IaC = 5 rows
    assert len(rows) == len(MAPPED_VERBS) + 1


# ===========================================================================
# Cross-platform contrast — one platform's anti-markers are the other's shape
# ===========================================================================
def test_cross_platform_contrast_holds_for_both_plugins() -> None:
    db = assert_target_matches_mapping(_db_plugin(), _databricks_pipeline())
    ecs = assert_target_matches_mapping(_ecs_plugin(), _aws_ecs_pipeline())
    # the different-shape proof: the encoded table forbids each other's shape
    assert_cross_platform_contrast([db, ecs])


def test_databricks_plugin_fails_the_aws_ecs_column() -> None:
    """A plugin cannot satisfy BOTH columns — databricks fails the aws-ecs column.

    We deliberately validate the databricks plugin against the aws-ecs §8b column
    by loading it under the aws-ecs mapping; it must raise because the databricks
    shape (wheel/DBFS/job) does not contain ``docker build`` and does contain the
    aws-ecs column's anti-markers. This is the inverse proof of the contrast.
    """
    from cicdgen.validate import target_mapping as tm

    db = _db_plugin()
    pipeline = _databricks_pipeline()
    column = TARGET_MAPPING["aws-ecs"]
    # run the databricks build verb and assert it does NOT match the aws-ecs cell
    text = tm._run_verb_text(db, pipeline, MAPPED_VERBS[0])  # build
    with pytest.raises(TargetMappingValidationError):
        tm._assert_expectation("databricks-as-aws-ecs/build", text, column.verbs[MAPPED_VERBS[0]])


def test_aws_ecs_plugin_fails_the_databricks_column() -> None:
    """The symmetric inverse — the aws-ecs plugin fails the databricks column."""
    from cicdgen.validate import target_mapping as tm

    ecs = _ecs_plugin()
    pipeline = _aws_ecs_pipeline()
    column = TARGET_MAPPING["databricks"]
    text = tm._run_verb_text(ecs, pipeline, MAPPED_VERBS[0])  # build
    with pytest.raises(TargetMappingValidationError):
        tm._assert_expectation("aws-ecs-as-databricks/build", text, column.verbs[MAPPED_VERBS[0]])


# ===========================================================================
# Unknown platform / table completeness
# ===========================================================================
def test_unknown_platform_is_rejected() -> None:
    """A plugin whose name has no §8b column encoded fails loudly."""
    db = _db_plugin()
    # monkey-free: temporarily present an unknown name via a tiny shim object
    class _Shim:
        name = "no-such-target"

        def __getattr__(self, item):
            return getattr(db, item)

    with pytest.raises(TargetMappingValidationError):
        assert_target_matches_mapping(_Shim(), _databricks_pipeline())  # type: ignore[arg-type]


def test_table_has_a_column_for_both_shipped_plugins() -> None:
    """Both shipped target plugins must have an encoded §8b column."""
    assert "aws-ecs" in TARGET_MAPPING
    assert "databricks" in TARGET_MAPPING
    # every column carries all four mapped verbs
    for name, column in TARGET_MAPPING.items():
        for verb in MAPPED_VERBS:
            assert verb in column.verbs, f"{name} column missing verb {verb}"
