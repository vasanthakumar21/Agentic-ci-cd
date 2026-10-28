"""End-to-end sample generation + validation for the Azure DevOps plugin (task 7.2, R3.2).

Task 7.2 asks us to "generate + validate against the CI mapping table (design
§8a)". This module covers that locally and deterministically, mirroring
``tests/test_github_sample_generation.py``:

    1. Load the committed sample IR ``examples/client-azdo-databricks/pipeline.yaml``
       (ci_vendor: azure-devops × target_platform: databricks — environments, a
       manual prod gate with approvals, a scan stage, Azure cloud OIDC, timeouts).
    2. Generate the Azure Pipelines file from it through the real ``azure-devops``
       CI-vendor plugin (no mocks).
    3. Prove the committed artifact
       ``examples/client-azdo-databricks/azure-pipelines.yml`` matches a fresh
       generation **byte-for-byte** (determinism, R5.3) — so the reviewable file
       in the repo is exactly what the generator produces.
    4. Validate the emitted YAML rigorously: it must parse with PyYAML and pass
       the structural §8a Azure DevOps checks
       (:func:`cicdgen.validate.validate_azure_pipeline_structure`), and every
       §8a mapping-table row must be represented
       (:func:`cicdgen.validate.assert_mapping_table_rows_represented`).

Actually *running* the pipeline in a throwaway Azure DevOps project is an
irreducible manual step (a real org, service connections, environment approvals,
and secrets are required). It is documented as a runbook in
``examples/client-azdo-databricks/README.md`` and is intentionally NOT executed
here (no network/account operations — R9 isolation).

Note on OIDC: this sample's ``cloud.provider`` is ``azure``, so the workload
identity federation step is ``AzureCLI@2`` with an ``azureSubscription`` service
connection (``azure-wif``) — not the AWS ``AWSShellScript@1`` variant.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from cicdgen.ir import Pipeline
from cicdgen.plugins import StageGraph, load_ci_vendor_plugin
from cicdgen.validate import (
    assert_mapping_table_rows_represented,
    validate_azure_pipeline_structure,
)

# <repo>/tests/test_azdo_sample_generation.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"
SAMPLE_DIR = REPO_ROOT / "examples" / "client-azdo-databricks"
SAMPLE_IR = SAMPLE_DIR / "pipeline.yaml"
COMMITTED_PIPELINE = SAMPLE_DIR / "azure-pipelines.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_sample_pipeline() -> Pipeline:
    """Parse + validate the committed sample IR into a Pipeline."""
    data = yaml.safe_load(SAMPLE_IR.read_text(encoding="utf-8"))
    return Pipeline.model_validate(data)


def _generate_pipeline_content() -> str:
    """Generate the pipeline content from the sample IR via the real plugin."""
    pipeline = _load_sample_pipeline()
    plugin = load_ci_vendor_plugin("azure-devops", plugins_root=PLUGINS_ROOT)
    files = plugin.emit_pipeline_files(StageGraph.from_pipeline(pipeline))
    assert len(files) == 1
    f = files[0]
    assert f.path == "azure-pipelines.yml"
    return f.content


def _stage_by_name(doc: dict, name: str) -> dict | None:
    for s in doc["stages"]:
        if s.get("stage") == name:
            return s
    return None


# ---------------------------------------------------------------------------
# Artifacts exist
# ---------------------------------------------------------------------------
def test_sample_ir_and_committed_artifact_exist() -> None:
    assert SAMPLE_IR.is_file(), f"missing sample IR: {SAMPLE_IR}"
    assert COMMITTED_PIPELINE.is_file(), f"missing committed pipeline: {COMMITTED_PIPELINE}"


# ---------------------------------------------------------------------------
# Sample IR is valid and exercises the task-7.2 concerns
# ---------------------------------------------------------------------------
def test_sample_ir_is_valid_azdo_databricks() -> None:
    pipeline = _load_sample_pipeline()
    assert str(pipeline.ci_vendor) == "azure-devops"
    assert str(pipeline.target_platform) == "databricks"
    # manual prod gate present with approvals
    prod = next(e for e in pipeline.environments if e.name == "prod")
    assert str(prod.gate) == "manual"
    # a stage requests scans, and Azure OIDC is configured
    assert any(s.scans for s in pipeline.stages)
    assert pipeline.cloud is not None
    assert str(pipeline.cloud.provider) == "azure"
    assert str(pipeline.cloud.auth) == "oidc"


# ---------------------------------------------------------------------------
# Committed artifact matches a fresh generation byte-for-byte (determinism R5.3)
# ---------------------------------------------------------------------------
def test_committed_artifact_matches_fresh_generation_byte_for_byte() -> None:
    fresh = _generate_pipeline_content()
    committed = COMMITTED_PIPELINE.read_bytes().decode("utf-8")
    assert committed == fresh, (
        "committed azure-pipelines.yml is stale — regenerate it from "
        "examples/client-azdo-databricks/pipeline.yaml so the reviewable artifact "
        "matches the generator output."
    )


def test_generation_is_deterministic_across_calls() -> None:
    assert _generate_pipeline_content() == _generate_pipeline_content()


# ---------------------------------------------------------------------------
# YAML parses and has the expected shape
# ---------------------------------------------------------------------------
def test_emitted_yaml_parses() -> None:
    doc = yaml.safe_load(_generate_pipeline_content())
    assert isinstance(doc, dict)
    assert "trigger" in doc
    assert isinstance(doc["stages"], list) and doc["stages"]
    assert isinstance(doc["variables"], list) and doc["variables"]


def test_committed_artifact_parses_too() -> None:
    doc = yaml.safe_load(COMMITTED_PIPELINE.read_text(encoding="utf-8"))
    names = [s["stage"] for s in doc["stages"]]
    # security (scan-only stage folded in) heads the chain, then declared order.
    assert names == ["security", "test", "build", "deploy_dev", "deploy_prod"]


# ---------------------------------------------------------------------------
# Structural §8a validation (always runs, no network)
# ---------------------------------------------------------------------------
def test_emitted_pipeline_passes_structural_validation() -> None:
    content = _generate_pipeline_content()
    doc = yaml.safe_load(content)
    # raises AzurePipelineValidationError on any violation
    validate_azure_pipeline_structure(doc, raw_text=content)


def test_committed_pipeline_passes_structural_validation() -> None:
    content = COMMITTED_PIPELINE.read_text(encoding="utf-8")
    doc = yaml.safe_load(content)
    validate_azure_pipeline_structure(doc, raw_text=content)


# ---------------------------------------------------------------------------
# Every §8a mapping-table row is represented
# ---------------------------------------------------------------------------
def test_all_mapping_table_rows_represented() -> None:
    doc = yaml.safe_load(_generate_pipeline_content())
    results = assert_mapping_table_rows_represented(
        doc,
        expect_manual_gate=True,  # prod is a manual-gated environment
        expect_timeout=True,      # test (15) / build (30) set timeouts
        expect_oidc=True,         # cloud.auth == oidc (azure WIF)
    )
    # Every asserted row came back True.
    assert all(results.values()), results
    assert set(results) == {
        "stage order (dependsOn)",
        "manual gate / approvals (env deployment job)",
        "timeout (timeoutInMinutes)",
        "secrets ref (variable group / $(X))",
        "cloud OIDC (WIF service connection)",
    }


# ---------------------------------------------------------------------------
# Row-by-row structural invariants for THIS sample (design §8a)
# ---------------------------------------------------------------------------
def test_row_stage_order_via_depends_on() -> None:
    doc = yaml.safe_load(_generate_pipeline_content())
    # security heads the chain (scan-only stage folded in); linear dependsOn after.
    assert "dependsOn" not in _stage_by_name(doc, "security")
    assert _stage_by_name(doc, "test")["dependsOn"] == "security"
    assert _stage_by_name(doc, "build")["dependsOn"] == "test"
    assert _stage_by_name(doc, "deploy_dev")["dependsOn"] == "build"
    assert _stage_by_name(doc, "deploy_prod")["dependsOn"] == "deploy_dev"


def test_row_scan_only_stage_folded_not_duplicated() -> None:
    """The IR stage id 'security' (scans, no runs) is folded into the single
    synthetic security stage — not emitted a second time (would duplicate the
    stage name and run an empty command)."""
    doc = yaml.safe_load(_generate_pipeline_content())
    names = [s["stage"] for s in doc["stages"]]
    assert names.count("security") == 1
    sec_steps = _stage_by_name(doc, "security")["jobs"][0]["steps"]
    step_names = [s.get("displayName") for s in sec_steps]
    assert "SAST scan" in step_names
    assert "Secret scan" in step_names
    assert "Dependency scan" in step_names


def test_row_manual_gate_via_environment_deployment_job() -> None:
    doc = yaml.safe_load(_generate_pipeline_content())
    prod_job = _stage_by_name(doc, "deploy_prod")["jobs"][0]
    assert "deployment" in prod_job
    assert prod_job["environment"] == "prod"
    assert "runOnce" in prod_job["strategy"]
    dev_job = _stage_by_name(doc, "deploy_dev")["jobs"][0]
    assert dev_job["environment"] == "dev"
    assert "deployment" in dev_job


def test_row_timeout_in_minutes() -> None:
    doc = yaml.safe_load(_generate_pipeline_content())
    assert _stage_by_name(doc, "test")["jobs"][0]["timeoutInMinutes"] == 15
    assert _stage_by_name(doc, "build")["jobs"][0]["timeoutInMinutes"] == 30


def test_row_secrets_variable_group_and_no_literals() -> None:
    content = _generate_pipeline_content()
    assert "group: cicd-secrets" in content
    assert "arn:aws:iam" not in content


def test_row_cloud_oidc_azure_wif_login() -> None:
    doc = yaml.safe_load(_generate_pipeline_content())
    # azure provider -> AzureCLI@2 with the azure-wif service connection.
    test_steps = _stage_by_name(doc, "test")["jobs"][0]["steps"]
    azure_steps = [s for s in test_steps if s.get("task") == "AzureCLI@2"]
    assert azure_steps, "expected an AzureCLI@2 WIF login step"
    assert azure_steps[0]["inputs"]["azureSubscription"] == "azure-wif"
    # the AWS variant must NOT appear for an azure-provider pipeline.
    tasks = [s.get("task") for s in test_steps]
    assert "AWSShellScript@1" not in tasks


def test_committed_artifact_secrets_only_by_reference() -> None:
    content = COMMITTED_PIPELINE.read_text(encoding="utf-8")
    assert "group: cicd-secrets" in content
    assert "arn:aws:iam" not in content
