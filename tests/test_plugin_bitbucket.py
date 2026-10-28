"""Unit tests for the Bitbucket Pipelines CI-vendor plugin (task 12.1, R3.4, §8a).

Loads the real ``plugins/ci/bitbucket`` package via the loader (no mocks, no
stand-in plugin), builds a :class:`StageGraph` from representative IRs, calls
``emit_pipeline_files``, and parses the emitted ``bitbucket-pipelines.yml`` with
PyYAML to assert the design §8a Bitbucket mapping holds:

    * a top-level ``pipelines`` key with an ordered ``default`` step sequence;
    * one ``- step:`` per *enabled* stage, in declared order (scan-only stages
      fold into the single leading ``security`` step);
    * a disabled stage produces no step;
    * a manual stage / a manual-gated environment yields ``trigger: manual``;
    * an env-targeted stage yields ``deployment: <env>``;
    * ``max-time`` is set from ``stage.timeout_minutes``;
    * a ``security`` step exists with one command per requested scan;
    * ``oidc: true`` + a provider login command appear under cloud OIDC;
    * secrets appear only as ``$NAME`` references — no literal values;
    * output is deterministic — two calls yield byte-identical content.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from cicdgen.ir import Pipeline
from cicdgen.plugins import GeneratedFile, StageGraph, load_ci_vendor_plugin

# plugins/ live at <repo>/plugins; this test file is <repo>/tests/test_plugin_bitbucket.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_ci_vendor_plugin("bitbucket", plugins_root=PLUGINS_ROOT)


def _emit(pipeline: Pipeline) -> GeneratedFile:
    graph = StageGraph.from_pipeline(pipeline)
    files = _plugin().emit_pipeline_files(graph)
    assert len(files) == 1
    return files[0]


def _doc(pipeline: Pipeline) -> dict:
    content = _emit(pipeline).content
    return yaml.safe_load(content)


def _default_steps(doc: dict) -> list[dict]:
    """Return the inner step mappings (unwrapping each ``{"step": {...}}``)."""
    return [entry["step"] for entry in doc["pipelines"]["default"]]


def _step_by_name(doc: dict, name: str) -> dict | None:
    for step in _default_steps(doc):
        if step.get("name") == name:
            return step
    return None


def _full_pipeline() -> Pipeline:
    """A representative IR exercising order, gates, timeout, scans, and OIDC."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "bitbucket",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "cloud": {"provider": "aws", "region": "eu-west-2", "auth": "oidc"},
            "environments": [
                {"name": "dev", "gate": "auto"},
                {"name": "prod", "gate": "manual", "protection": "required-approvals"},
            ],
            "stages": [
                {"id": "test", "runs": "make test", "timeout_minutes": 15,
                 "scans": ["sast", "secrets"]},
                {"id": "build", "runs": "make build", "timeout_minutes": 30},
                {"id": "deploy-dev", "runs": "make deploy", "environment": "dev"},
                {"id": "deploy-prod", "runs": "make deploy", "environment": "prod",
                 "approvals": {"required": 2, "from_group": "leads"}},
            ],
        }
    )


# ===========================================================================
# Loader / identity
# ===========================================================================
def test_plugin_loads_by_name() -> None:
    plugin = _plugin()
    assert plugin.name == "bitbucket"


def test_emits_single_pipeline_file() -> None:
    f = _emit(_full_pipeline())
    assert f.path == "bitbucket-pipelines.yml"
    assert not f.executable


# ===========================================================================
# Top-level structure: pipelines -> default -> ordered step sequence
# ===========================================================================
def test_top_level_pipelines_with_ordered_step_sequence() -> None:
    doc = _doc(_full_pipeline())
    assert "pipelines" in doc
    assert "default" in doc["pipelines"]
    steps = doc["pipelines"]["default"]
    assert isinstance(steps, list) and steps
    # every entry is a {"step": {...}} mapping
    for entry in steps:
        assert isinstance(entry, dict) and "step" in entry


def test_one_step_per_enabled_stage_in_declared_order() -> None:
    doc = _doc(_full_pipeline())
    names = [s["name"] for s in _default_steps(doc)]
    # leading synthetic security step, then the four stage steps in order
    assert names == ["security", "test", "build", "deploy-dev", "deploy-prod"]


def test_disabled_stage_produces_no_step() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "bitbucket",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [
                {"id": "test", "runs": "t"},
                {"id": "build", "runs": "b", "enabled": False},
                {"id": "deploy", "runs": "d"},
            ],
        }
    )
    names = [s["name"] for s in _default_steps(_doc(ir))]
    assert "build" not in names
    assert names == ["test", "deploy"]


def test_step_script_carries_stage_command() -> None:
    doc = _doc(_full_pipeline())
    build = _step_by_name(doc, "build")
    assert build is not None
    assert "make build" in build["script"]


# ===========================================================================
# Manual gates (trigger: manual) / deployment env
# ===========================================================================
def test_env_stage_has_deployment_key() -> None:
    doc = _doc(_full_pipeline())
    assert _step_by_name(doc, "deploy-dev")["deployment"] == "dev"
    assert _step_by_name(doc, "deploy-prod")["deployment"] == "prod"


def test_manual_gated_env_stage_has_trigger_manual() -> None:
    doc = _doc(_full_pipeline())
    # prod is a manual-gated environment -> trigger: manual
    assert _step_by_name(doc, "deploy-prod")["trigger"] == "manual"
    # dev is auto-gated -> no manual trigger
    assert "trigger" not in _step_by_name(doc, "deploy-dev")


def test_manual_stage_without_env_still_gated() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "bitbucket",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [
                {"id": "test", "runs": "t"},
                {"id": "release", "runs": "r", "when": "manual"},
            ],
        }
    )
    release = _step_by_name(_doc(ir), "release")
    assert release["trigger"] == "manual"


# ===========================================================================
# Timeouts (max-time)
# ===========================================================================
def test_max_time_mapped_from_timeout() -> None:
    doc = _doc(_full_pipeline())
    assert _step_by_name(doc, "test")["max-time"] == 15
    assert _step_by_name(doc, "build")["max-time"] == 30
    # a stage with no timeout omits the key
    assert "max-time" not in _step_by_name(doc, "deploy-dev")


# ===========================================================================
# Security / scans
# ===========================================================================
def test_security_step_present_with_requested_scans() -> None:
    doc = _doc(_full_pipeline())
    security = _step_by_name(doc, "security")
    assert security is not None
    script = security["script"]
    assert any("SAST" in cmd for cmd in script)
    assert any("secret scan" in cmd for cmd in script)
    # dependency was not requested -> not present
    assert not any("dependency scan" in cmd for cmd in script)


def test_no_security_step_when_no_scans() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "bitbucket",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [{"id": "test", "runs": "t"}],
        }
    )
    names = [s["name"] for s in _default_steps(_doc(ir))]
    assert "security" not in names


# ===========================================================================
# Cloud OIDC
# ===========================================================================
def test_oidc_flag_and_aws_login_command() -> None:
    doc = _doc(_full_pipeline())
    build = _step_by_name(doc, "build")
    assert build["oidc"] is True
    # the aws assume-role command appears in the script, referencing the role var
    assert any("assume-role-with-web-identity" in cmd for cmd in build["script"])
    assert any("$AWS_DEPLOY_ROLE_ARN" in cmd for cmd in build["script"])


def test_oidc_azure_login_command() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "bitbucket",
            "target_platform": "azure-functions",
            "project": {"name": "demo"},
            "cloud": {"provider": "azure", "auth": "oidc"},
            "stages": [{"id": "deploy", "runs": "d"}],
        }
    )
    deploy = _step_by_name(_doc(ir), "deploy")
    assert deploy["oidc"] is True
    assert any("az login" in cmd for cmd in deploy["script"])
    assert any("$AZURE_CLIENT_ID" in cmd for cmd in deploy["script"])


def test_no_oidc_flag_when_keys_auth() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "bitbucket",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "cloud": {"provider": "aws", "auth": "keys"},
            "stages": [{"id": "deploy", "runs": "d"}],
        }
    )
    deploy = _step_by_name(_doc(ir), "deploy")
    assert "oidc" not in deploy
    assert not any("assume-role-with-web-identity" in cmd for cmd in deploy["script"])


# ===========================================================================
# Secrets by reference only ($NAME, R6.3/R6.6)
# ===========================================================================
def test_secrets_only_by_reference_no_literals() -> None:
    content = _emit(_full_pipeline()).content
    # the role is referenced as a $NAME variable, never a literal ARN
    assert "$AWS_DEPLOY_ROLE_ARN" in content
    assert "arn:aws:iam" not in content
    # no obvious literal credential assignments
    assert not re.search(r"(?i)password\s*[:=]\s*\S+", content)


# ===========================================================================
# Determinism (R5.3)
# ===========================================================================
def test_output_is_byte_identical_across_calls() -> None:
    ir = _full_pipeline()
    first = _emit(ir).content
    second = _emit(ir).content
    assert first == second


def test_output_identical_across_fresh_plugin_instances() -> None:
    ir = _full_pipeline()
    a = _plugin().emit_pipeline_files(StageGraph.from_pipeline(ir))[0].content
    b = _plugin().emit_pipeline_files(StageGraph.from_pipeline(ir))[0].content
    assert a == b


# ===========================================================================
# Emitted content is valid, non-empty YAML
# ===========================================================================
def test_emitted_yaml_parses_and_has_expected_top_level() -> None:
    doc = _doc(_full_pipeline())
    assert isinstance(doc, dict)
    assert doc["image"]
    assert isinstance(doc["pipelines"], dict)
    assert isinstance(doc["pipelines"]["default"], list) and doc["pipelines"]["default"]
