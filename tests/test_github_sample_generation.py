"""End-to-end sample generation + validation for the GitHub plugin (task 6.2, R3.2).

Task 6.2 asks us to "generate for a sample IR; validate the YAML; run in the
isolated account". This module covers the first two, locally and deterministically:

    1. Load the committed sample IR ``examples/client-github-aws/pipeline.yaml``
       (ci_vendor: github × target_platform: aws-ecs — environments, a manual
       prod gate, security scans, cloud OIDC, timeouts).
    2. Generate the GitHub Actions workflow from it through the real ``github``
       CI-vendor plugin (no mocks).
    3. Prove the committed artifact
       ``examples/client-github-aws/.github/workflows/ci.yml`` matches a fresh
       generation **byte-for-byte** (determinism, R5.3) — so the reviewable file
       in the repo is exactly what the generator produces.
    4. Validate the emitted YAML rigorously: it must parse with PyYAML and pass
       the structural GitHub Actions checks (:mod:`cicdgen.validate`). When the
       SchemaStore workflow schema can be fetched it is additionally validated
       against that published JSON Schema; offline, that step is skipped (the
       structural check still runs), matching the isolated-environment posture.

The third leg of the task — actually *running* the workflow in a throwaway
GitHub account — is an irreducible manual step (a real account, reviewers, and
secrets are required). It is documented as a runbook in
``examples/client-github-aws/README.md`` and is intentionally NOT executed here
(no network/account operations against a real org — R9 isolation).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cicdgen.ir import Pipeline
from cicdgen.plugins import StageGraph, load_ci_vendor_plugin
from cicdgen.validate import (
    fetch_github_workflow_schema,
    validate_against_schema,
    validate_workflow_structure,
)

# <repo>/tests/test_github_sample_generation.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"
SAMPLE_DIR = REPO_ROOT / "examples" / "client-github-aws"
SAMPLE_IR = SAMPLE_DIR / "pipeline.yaml"
COMMITTED_WORKFLOW = SAMPLE_DIR / ".github" / "workflows" / "ci.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_sample_pipeline() -> Pipeline:
    """Parse + validate the committed sample IR into a Pipeline."""
    data = yaml.safe_load(SAMPLE_IR.read_text(encoding="utf-8"))
    return Pipeline.model_validate(data)


def _generate_workflow_content() -> str:
    """Generate the workflow content from the sample IR via the real plugin."""
    pipeline = _load_sample_pipeline()
    plugin = load_ci_vendor_plugin("github", plugins_root=PLUGINS_ROOT)
    files = plugin.emit_pipeline_files(StageGraph.from_pipeline(pipeline))
    assert len(files) == 1
    f = files[0]
    assert f.path == ".github/workflows/ci.yml"
    return f.content


# ---------------------------------------------------------------------------
# Fixtures that assert the artifacts exist up front
# ---------------------------------------------------------------------------
def test_sample_ir_and_committed_artifact_exist() -> None:
    assert SAMPLE_IR.is_file(), f"missing sample IR: {SAMPLE_IR}"
    assert COMMITTED_WORKFLOW.is_file(), f"missing committed workflow: {COMMITTED_WORKFLOW}"


# ---------------------------------------------------------------------------
# Sample IR is valid and exercises the task-6.2 concerns
# ---------------------------------------------------------------------------
def test_sample_ir_is_valid_github_aws() -> None:
    pipeline = _load_sample_pipeline()
    assert str(pipeline.ci_vendor) == "github"
    assert str(pipeline.target_platform) == "aws-ecs"
    # manual prod gate present
    prod = next(e for e in pipeline.environments if e.name == "prod")
    assert str(prod.gate) == "manual"
    # a stage requests scans, and OIDC is configured
    assert any(s.scans for s in pipeline.stages)
    assert pipeline.cloud is not None and str(pipeline.cloud.auth) == "oidc"


# ---------------------------------------------------------------------------
# Committed artifact matches a fresh generation byte-for-byte (determinism)
# ---------------------------------------------------------------------------
def test_committed_artifact_matches_fresh_generation_byte_for_byte() -> None:
    fresh = _generate_workflow_content()
    # Read bytes (no newline translation) so the comparison is truly byte-for-byte;
    # the generator writes with newline="" (no translation) too.
    committed = COMMITTED_WORKFLOW.read_bytes().decode("utf-8")
    assert committed == fresh, (
        "committed .github/workflows/ci.yml is stale — regenerate it from "
        "examples/client-github-aws/pipeline.yaml so the reviewable artifact "
        "matches the generator output."
    )


def test_generation_is_deterministic_across_calls() -> None:
    assert _generate_workflow_content() == _generate_workflow_content()


# ---------------------------------------------------------------------------
# YAML parses and has the expected shape
# ---------------------------------------------------------------------------
def test_emitted_yaml_parses() -> None:
    wf = yaml.safe_load(_generate_workflow_content())
    assert isinstance(wf, dict)
    assert wf["name"] == "orders-api CI"
    # `on` survives as a string key, not the YAML-1.1 boolean True
    assert "on" in wf and True not in wf
    assert isinstance(wf["jobs"], dict) and wf["jobs"]


def test_committed_artifact_parses_too() -> None:
    wf = yaml.safe_load(COMMITTED_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(wf, dict)
    assert set(wf["jobs"]) == {"security", "test", "build", "deploy-dev", "deploy-prod"}


# ---------------------------------------------------------------------------
# Structural validation (always runs, no network)
# ---------------------------------------------------------------------------
def test_emitted_workflow_passes_structural_validation() -> None:
    wf = yaml.safe_load(_generate_workflow_content())
    # raises GitHubWorkflowValidationError on any violation
    validate_workflow_structure(wf)


def test_structural_invariants_for_this_sample() -> None:
    wf = yaml.safe_load(_generate_workflow_content())
    jobs = wf["jobs"]
    # needs chain preserves declared order over the security side-job
    assert "needs" not in jobs["test"]
    assert jobs["build"]["needs"] == "test"
    assert jobs["deploy-dev"]["needs"] == "build"
    assert jobs["deploy-prod"]["needs"] == "deploy-dev"
    # manual prod gate -> environment reference
    assert jobs["deploy-prod"]["environment"] == "prod"
    assert jobs["deploy-dev"]["environment"] == "dev"
    # timeouts mapped
    assert jobs["test"]["timeout-minutes"] == 15
    assert jobs["build"]["timeout-minutes"] == 30
    # OIDC: id-token granted and an aws credentials step present
    assert wf["permissions"]["id-token"] == "write"
    uses = [s.get("uses") for s in jobs["deploy-prod"]["steps"]]
    assert any(u and "aws-actions/configure-aws-credentials" in u for u in uses)
    # security job carries the requested scans
    scan_steps = [s.get("name") for s in jobs["security"]["steps"]]
    assert "SAST scan" in scan_steps and "Secret scan" in scan_steps


def test_secrets_only_by_reference_in_committed_artifact() -> None:
    content = COMMITTED_WORKFLOW.read_text(encoding="utf-8")
    assert "${{ secrets.AWS_DEPLOY_ROLE_ARN }}" in content
    assert "arn:aws:iam" not in content  # never a literal ARN


# ---------------------------------------------------------------------------
# Schema validation against the published GitHub Actions schema (if reachable)
# ---------------------------------------------------------------------------
def test_emitted_workflow_passes_schema_when_reachable() -> None:
    schema = fetch_github_workflow_schema()
    if schema is None:
        pytest.skip(
            "SchemaStore GitHub workflow schema unreachable (offline/isolated) — "
            "structural validation already covers the workflow."
        )
    wf = yaml.safe_load(_generate_workflow_content())
    validate_against_schema(wf, schema)  # raises on any schema violation
