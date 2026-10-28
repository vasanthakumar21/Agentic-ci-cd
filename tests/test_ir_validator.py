"""Unit tests for the IR validator (task 2.3).

Covers the fail-fast contract of ``cicdgen.ir`` (R1, R2, R3.1):

    * a valid IR (the sample) passes — including the separate plugin-existence
      check, exercised with an injected known-plugin set;
    * a bad enum fails with a message listing the valid values;
    * an unknown/extra field fails ("Extra inputs are not permitted") naming the
      offending field path;
    * a missing required field fails naming the field;
    * a valid-shape IR naming a plugin that does not exist fails with an
      IRValidationError (PluginNotFoundError) naming the missing plugin;
    * a cross-field rule — a stage targeting an undeclared environment — fails;
    * omitted optional knobs resolve to their documented defaults (R2.2).

The plugin-existence layer is exercised deterministically by injecting an
``AvailablePlugins`` known set, so these tests never depend on the (still-empty)
real ``plugins/`` tree.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from cicdgen.ir import (
    AvailablePlugins,
    CloudAuth,
    GateMode,
    IRValidationError,
    Pipeline,
    PluginNotFoundError,
    Protection,
    WhenMode,
    check_plugins_exist,
    load_ir_dict,
    validate_ir,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_IR = REPO_ROOT / "examples" / "client-azdo-databricks" / "pipeline.yaml"

# The sample IR names ci_vendor=azure-devops x target_platform=databricks. An
# injected known-plugin set lets the existence check succeed without the real
# plugins/ tree being populated (plugins land in tasks 6-9).
KNOWN_PLUGINS = AvailablePlugins(
    ci=frozenset({"github", "azure-devops"}),
    target=frozenset({"aws-ecs", "databricks"}),
)


@pytest.fixture
def sample_dict() -> dict:
    """The sample IR as a fresh, mutable dict for each test."""
    return load_ir_dict(SAMPLE_IR)


# ---------------------------------------------------------------------------
# 1. Valid IR passes
# ---------------------------------------------------------------------------
def test_valid_ir_passes(sample_dict: dict) -> None:
    """The sample IR validates cleanly, including the injected plugin check."""
    model = validate_ir(
        sample_dict,
        source=str(SAMPLE_IR),
        check_plugins=True,
        available_plugins=KNOWN_PLUGINS,
    )
    assert isinstance(model, Pipeline)
    assert model.version == 1
    assert model.ci_vendor.value == "azure-devops"
    assert model.target_platform.value == "databricks"
    assert model.project.name == "my-service"
    assert [s.id for s in model.stages] == [
        "test",
        "security",
        "build",
        "deploy-dev",
        "deploy-prod",
    ]


# ---------------------------------------------------------------------------
# 2. Bad enum fails, listing valid values
# ---------------------------------------------------------------------------
def test_bad_enum_fails_listing_valid_values(sample_dict: dict) -> None:
    """A bad ci_vendor enum fails naming the field and listing valid values."""
    sample_dict["ci_vendor"] = "foo"

    with pytest.raises(IRValidationError) as exc_info:
        validate_ir(sample_dict, available_plugins=KNOWN_PLUGINS)

    msg = str(exc_info.value)
    assert "ci_vendor" in msg
    # Pydantic enum errors enumerate the permitted members.
    assert "github" in msg
    assert "azure-devops" in msg


# ---------------------------------------------------------------------------
# 3. Unknown / extra field fails
# ---------------------------------------------------------------------------
def test_extra_top_level_field_fails(sample_dict: dict) -> None:
    """An unknown top-level key is rejected (extra='forbid')."""
    sample_dict["bogus_field"] = "nope"

    with pytest.raises(IRValidationError) as exc_info:
        validate_ir(sample_dict, available_plugins=KNOWN_PLUGINS)

    msg = str(exc_info.value)
    assert "Extra inputs are not permitted" in msg
    assert "bogus_field" in msg


def test_extra_nested_field_fails_with_dotted_path(sample_dict: dict) -> None:
    """An unknown nested key is rejected with its dotted field path."""
    sample_dict["stages"][0]["unexpected"] = True

    with pytest.raises(IRValidationError) as exc_info:
        validate_ir(sample_dict, available_plugins=KNOWN_PLUGINS)

    msg = str(exc_info.value)
    assert "Extra inputs are not permitted" in msg
    # Dotted path names the offending nested location.
    assert "stages.0.unexpected" in msg


# ---------------------------------------------------------------------------
# 4. Missing required field fails naming the field
# ---------------------------------------------------------------------------
def test_missing_required_stages_fails(sample_dict: dict) -> None:
    """Dropping the required 'stages' key fails naming the field."""
    del sample_dict["stages"]

    with pytest.raises(IRValidationError) as exc_info:
        validate_ir(sample_dict, available_plugins=KNOWN_PLUGINS)

    msg = str(exc_info.value)
    assert "stages" in msg
    assert "Field required" in msg


def test_missing_required_project_fails(sample_dict: dict) -> None:
    """Dropping the required 'project' key fails naming the field."""
    del sample_dict["project"]

    with pytest.raises(IRValidationError) as exc_info:
        validate_ir(sample_dict, available_plugins=KNOWN_PLUGINS)

    msg = str(exc_info.value)
    assert "project" in msg
    assert "Field required" in msg


# ---------------------------------------------------------------------------
# 5. Unknown plugin — valid shape, but the named plugin does not exist
# ---------------------------------------------------------------------------
def test_unknown_plugin_fails_naming_it(sample_dict: dict) -> None:
    """A valid IR whose ci_vendor plugin is absent fails naming the plugin."""
    # Known set deliberately omits 'azure-devops' on the ci axis.
    only_github = AvailablePlugins(
        ci=frozenset({"github"}),
        target=frozenset({"aws-ecs", "databricks"}),
    )

    with pytest.raises(IRValidationError) as exc_info:
        validate_ir(sample_dict, available_plugins=only_github)

    msg = str(exc_info.value)
    assert "azure-devops" in msg
    assert "no plugin" in msg


def test_check_plugins_exist_raises_plugin_not_found() -> None:
    """The lower-level check raises PluginNotFoundError naming the missing one."""
    with pytest.raises(PluginNotFoundError) as exc_info:
        check_plugins_exist(
            "azure-devops",
            "kubernetes",  # not in the known target set
            available=KNOWN_PLUGINS,
        )

    msg = str(exc_info.value)
    assert "kubernetes" in msg
    assert "target_platform" in msg


# ---------------------------------------------------------------------------
# 6. Cross-field: stage.environment must name a declared environment
# ---------------------------------------------------------------------------
def test_stage_targets_undeclared_environment_fails(sample_dict: dict) -> None:
    """A stage pointing at an undeclared environment fails the cross-field rule."""
    sample_dict["stages"][3]["environment"] = "staging"  # not in environments

    with pytest.raises(IRValidationError) as exc_info:
        validate_ir(sample_dict, available_plugins=KNOWN_PLUGINS)

    msg = str(exc_info.value)
    assert "staging" in msg
    assert "not declared" in msg


# ---------------------------------------------------------------------------
# 7. Default resolution — omitted optional knobs resolve to documented defaults
# ---------------------------------------------------------------------------
def test_omitted_knobs_resolve_to_documented_defaults() -> None:
    """Minimal IR: optional knobs resolve to their documented defaults (R2.2)."""
    minimal = {
        "version": 1,
        "ci_vendor": "github",
        "target_platform": "aws-ecs",
        "project": {"name": "svc"},
        "environments": [{"name": "dev"}],
        "stages": [{"id": "build"}],
    }

    model = validate_ir(minimal, available_plugins=KNOWN_PLUGINS)

    # Stage defaults.
    stage = model.stages[0]
    assert stage.enabled is True
    assert stage.retries == 0
    assert stage.when is WhenMode.AUTO
    assert stage.timeout_minutes is None
    assert stage.approvals is None

    # Environment defaults.
    env = model.environments[0]
    assert env.gate is GateMode.AUTO
    assert env.protection is Protection.NONE

    # Optional top-level blocks default to None / empty.
    assert model.cloud is None
    assert model.thresholds is None
    assert model.notifications is None


def test_cloud_auth_defaults_to_oidc() -> None:
    """When cloud.auth is omitted, it resolves to oidc (R2.2)."""
    with_cloud = {
        "version": 1,
        "ci_vendor": "github",
        "target_platform": "aws-ecs",
        "project": {"name": "svc"},
        "stages": [{"id": "build"}],
        "cloud": {"provider": "aws"},
    }

    model = validate_ir(with_cloud, available_plugins=KNOWN_PLUGINS)

    assert model.cloud is not None
    assert model.cloud.auth is CloudAuth.OIDC


# ---------------------------------------------------------------------------
# Guard: mutating the sample dict in one test never leaks into another.
# ---------------------------------------------------------------------------
def test_sample_fixture_is_isolated(sample_dict: dict) -> None:
    """Sanity: the fixture hands each test an independent copy."""
    snapshot = copy.deepcopy(sample_dict)
    sample_dict["ci_vendor"] = "mutated"
    assert snapshot["ci_vendor"] == "azure-devops"
