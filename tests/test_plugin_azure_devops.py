"""Unit tests for the Azure DevOps CI-vendor plugin (task 7.1, R3.2, design §8a).

Loads the real ``plugins/ci/azure-devops`` package via the loader (no mocks, no
stand-in plugin), builds a :class:`StageGraph` from representative IRs, calls
``emit_pipeline_files``, and parses the emitted ``azure-pipelines.yml`` with
PyYAML to assert the design §8a Azure DevOps mapping holds:

    * one Azure stage per *enabled* IR stage, in declared order, chained by
      ``dependsOn``;
    * a disabled stage produces no stage (and the chain closes over it);
    * env-targeted / manual stages use a ``deployment`` job bound to an
      ``environment:`` (where approvals/checks are enforced);
    * ``timeoutInMinutes`` is set from ``stage.timeout_minutes``;
    * a ``security`` stage exists with a step per requested scan when scans asked;
    * a workload-identity / service-connection login step appears under cloud
      OIDC (aws/azure);
    * secrets appear only as ``$(X)`` references / service-connection names — no
      literal values;
    * output is deterministic — two calls yield byte-identical content.

The emitted YAML is also validated as *parseable* Azure Pipelines structure.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from cicdgen.ir import Pipeline
from cicdgen.plugins import GeneratedFile, StageGraph, load_ci_vendor_plugin

# plugins/ live at <repo>/plugins; this test file is <repo>/tests/test_plugin_azure_devops.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_ci_vendor_plugin("azure-devops", plugins_root=PLUGINS_ROOT)


def _emit(pipeline: Pipeline) -> GeneratedFile:
    graph = StageGraph.from_pipeline(pipeline)
    files = _plugin().emit_pipeline_files(graph)
    assert len(files) == 1
    return files[0]


def _doc(pipeline: Pipeline) -> dict:
    content = _emit(pipeline).content
    return yaml.safe_load(content)


def _stage_by_name(doc: dict, name: str) -> dict | None:
    for s in doc["stages"]:
        if s.get("stage") == name:
            return s
    return None


def _full_pipeline() -> Pipeline:
    """A representative IR exercising order, gates, timeout, scans, and OIDC."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "azure-devops",
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
    assert plugin.name == "azure-devops"


def test_emits_single_pipeline_file() -> None:
    f = _emit(_full_pipeline())
    assert f.path == "azure-pipelines.yml"
    assert not f.executable


# ===========================================================================
# Trigger + variable group
# ===========================================================================
def test_trigger_present() -> None:
    doc = _doc(_full_pipeline())
    assert doc["trigger"]["branches"]["include"] == ["main"]


def test_variable_group_referenced() -> None:
    doc = _doc(_full_pipeline())
    groups = [v.get("group") for v in doc["variables"]]
    assert "cicd-secrets" in groups


# ===========================================================================
# Stage order via stages + dependsOn
# ===========================================================================
def test_stages_exist_for_enabled_stages() -> None:
    doc = _doc(_full_pipeline())
    # azure stage names replace '-' with '_'
    for sname in ("test", "build", "deploy_dev", "deploy_prod"):
        assert _stage_by_name(doc, sname) is not None, f"missing stage {sname}"


def test_depends_on_chains_in_declared_order() -> None:
    doc = _doc(_full_pipeline())
    # security stage heads the chain (scans requested); first real stage depends on it
    assert _stage_by_name(doc, "security") is not None
    assert _stage_by_name(doc, "test")["dependsOn"] == "security"
    assert _stage_by_name(doc, "build")["dependsOn"] == "test"
    assert _stage_by_name(doc, "deploy_dev")["dependsOn"] == "build"
    assert _stage_by_name(doc, "deploy_prod")["dependsOn"] == "deploy_dev"


def test_stage_order_preserved_in_sequence() -> None:
    doc = _doc(_full_pipeline())
    names = [s["stage"] for s in doc["stages"]]
    # security first, then declared order
    assert names == ["security", "test", "build", "deploy_dev", "deploy_prod"]


def test_disabled_stage_produces_no_stage_and_chain_closes() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "azure-devops",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [
                {"id": "test", "runs": "t"},
                {"id": "build", "runs": "b", "enabled": False},
                {"id": "deploy", "runs": "d"},
            ],
        }
    )
    doc = _doc(ir)
    assert _stage_by_name(doc, "build") is None
    # no scans -> test is chain head (no dependsOn); deploy chains over disabled build onto test
    assert "dependsOn" not in _stage_by_name(doc, "test")
    assert _stage_by_name(doc, "deploy")["dependsOn"] == "test"


# ===========================================================================
# Manual gates / environments / approvals (deployment jobs)
# ===========================================================================
def test_env_stage_uses_deployment_job_with_environment() -> None:
    doc = _doc(_full_pipeline())
    dev_job = _stage_by_name(doc, "deploy_dev")["jobs"][0]
    prod_job = _stage_by_name(doc, "deploy_prod")["jobs"][0]
    assert dev_job["environment"] == "dev"
    assert prod_job["environment"] == "prod"
    # deployment jobs carry a runOnce strategy
    assert "deployment" in dev_job
    assert "runOnce" in prod_job["strategy"]


def test_non_env_stage_uses_plain_job() -> None:
    doc = _doc(_full_pipeline())
    build_job = _stage_by_name(doc, "build")["jobs"][0]
    assert "job" in build_job
    assert "environment" not in build_job


def test_manual_stage_without_env_still_gated() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "azure-devops",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [
                {"id": "test", "runs": "t"},
                {"id": "release", "runs": "r", "when": "manual"},
            ],
        }
    )
    doc = _doc(ir)
    rel_job = _stage_by_name(doc, "release")["jobs"][0]
    # manual stage binds to an environment so an approval check gates it
    assert rel_job["environment"] == "release"
    assert "deployment" in rel_job


# ===========================================================================
# Timeouts
# ===========================================================================
def test_timeout_minutes_mapped() -> None:
    doc = _doc(_full_pipeline())
    test_job = _stage_by_name(doc, "test")["jobs"][0]
    build_job = _stage_by_name(doc, "build")["jobs"][0]
    assert test_job["timeoutInMinutes"] == 15
    assert build_job["timeoutInMinutes"] == 30
    # a stage with no timeout omits the key
    dev_job = _stage_by_name(doc, "deploy_dev")["jobs"][0]
    assert "timeoutInMinutes" not in dev_job


# ===========================================================================
# Security / scans
# ===========================================================================
def test_security_stage_present_with_requested_scans() -> None:
    doc = _doc(_full_pipeline())
    sec = _stage_by_name(doc, "security")
    assert sec is not None
    steps = sec["jobs"][0]["steps"]
    step_names = [s.get("displayName") for s in steps]
    assert "SAST scan" in step_names
    assert "Secret scan" in step_names
    # dependency was not requested -> not present
    assert "Dependency scan" not in step_names


def test_no_security_stage_when_no_scans() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "azure-devops",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [{"id": "test", "runs": "t"}],
        }
    )
    doc = _doc(ir)
    assert _stage_by_name(doc, "security") is None


# ===========================================================================
# Cloud OIDC / workload identity federation
# ===========================================================================
def test_oidc_aws_service_connection_step() -> None:
    doc = _doc(_full_pipeline())
    steps = _stage_by_name(doc, "deploy_dev")["jobs"][0]["strategy"]["runOnce"]["deploy"]["steps"]
    tasks = [s.get("task") for s in steps]
    assert "AWSShellScript@1" in tasks
    # the AWS service connection is referenced by name (not a literal secret)
    aws_step = next(s for s in steps if s.get("task") == "AWSShellScript@1")
    assert aws_step["inputs"]["awsCredentials"] == "aws-oidc"


def test_oidc_azure_login_step() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "azure-devops",
            "target_platform": "azure-functions",
            "project": {"name": "demo"},
            "cloud": {"provider": "azure", "auth": "oidc"},
            "stages": [{"id": "deploy", "runs": "d"}],
        }
    )
    doc = _doc(ir)
    steps = _stage_by_name(doc, "deploy")["jobs"][0]["steps"]
    tasks = [s.get("task") for s in steps]
    assert "AzureCLI@2" in tasks
    azure_step = next(s for s in steps if s.get("task") == "AzureCLI@2")
    assert azure_step["inputs"]["azureSubscription"] == "azure-wif"


def test_no_oidc_step_when_keys_auth() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "azure-devops",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "cloud": {"provider": "aws", "auth": "keys"},
            "stages": [{"id": "deploy", "runs": "d"}],
        }
    )
    doc = _doc(ir)
    steps = _stage_by_name(doc, "deploy")["jobs"][0]["steps"]
    tasks = [s.get("task") for s in steps]
    assert "AWSShellScript@1" not in tasks


# ===========================================================================
# Secrets by reference only (R6.3/R6.6)
# ===========================================================================
def test_secrets_only_by_reference_no_literals() -> None:
    content = _emit(_full_pipeline()).content
    # no literal AWS role ARN
    assert "arn:aws:iam" not in content
    # no obvious literal credential assignments
    assert not re.search(r"(?i)password\s*[:=]\s*\S+", content)
    # the variable group is linked (where secret values live)
    assert "group: cicd-secrets" in content


def test_gcp_oidc_uses_macro_reference() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "azure-devops",
            "target_platform": "kubernetes",
            "project": {"name": "demo"},
            "cloud": {"provider": "gcp", "auth": "oidc"},
            "stages": [{"id": "deploy", "runs": "d"}],
        }
    )
    content = _emit(ir).content
    # GCP identifiers referenced via $(VAR) macro, never literal
    assert "$(GCP_WORKLOAD_IDENTITY_PROVIDER)" in content


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
    assert "trigger" in doc
    assert isinstance(doc["stages"], list) and doc["stages"]
    assert isinstance(doc["variables"], list) and doc["variables"]
