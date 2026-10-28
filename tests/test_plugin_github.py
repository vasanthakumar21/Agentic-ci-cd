"""Unit tests for the GitHub Actions CI-vendor plugin (task 6.1, R3.2, design §8a).

Loads the real ``plugins/ci/github`` package via the loader (no mocks, no stand-in
plugin), builds a :class:`StageGraph` from representative IRs, calls
``emit_pipeline_files``, and parses the emitted workflow with PyYAML to assert the
design §8a GitHub Actions mapping holds:

    * one job per *enabled* stage, in declared order, chained by ``needs``;
    * a disabled stage produces no job (and the chain closes over it);
    * a manual stage / a manual-gated environment yields an ``environment:`` key;
    * ``timeout-minutes`` is set from ``stage.timeout_minutes``;
    * a ``security`` job exists with a step per requested scan when scans are asked;
    * ``permissions: id-token: write`` + a provider credentials step appear under
      cloud OIDC (aws/azure);
    * secrets appear only as ``${{ secrets.X }}`` references — no literal values;
    * the top-level ``on:`` trigger key survives (not turned into ``true:``);
    * output is deterministic — two calls yield byte-identical content.

The emitted YAML is also validated as *parseable* GitHub Actions structure.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from cicdgen.ir import Pipeline
from cicdgen.plugins import GeneratedFile, StageGraph, load_ci_vendor_plugin

# plugins/ live at <repo>/plugins; this test file is <repo>/tests/test_plugin_github.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_ci_vendor_plugin("github", plugins_root=PLUGINS_ROOT)


def _emit(pipeline: Pipeline) -> GeneratedFile:
    graph = StageGraph.from_pipeline(pipeline)
    files = _plugin().emit_pipeline_files(graph)
    assert len(files) == 1
    return files[0]


def _workflow(pipeline: Pipeline) -> dict:
    content = _emit(pipeline).content
    return yaml.safe_load(content)


def _full_pipeline() -> Pipeline:
    """A representative IR exercising order, gates, timeout, scans, and OIDC."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
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
    assert plugin.name == "github"


def test_emits_single_workflow_file() -> None:
    f = _emit(_full_pipeline())
    assert f.path == ".github/workflows/ci.yml"
    assert not f.executable


# ===========================================================================
# Stage order via jobs + needs
# ===========================================================================
def test_jobs_exist_for_enabled_stages_in_order() -> None:
    wf = _workflow(_full_pipeline())
    jobs = wf["jobs"]
    # security job + four stage jobs
    for jid in ("test", "build", "deploy-dev", "deploy-prod"):
        assert jid in jobs, f"missing job {jid}"


def test_needs_chains_in_declared_order() -> None:
    wf = _workflow(_full_pipeline())
    jobs = wf["jobs"]
    # first stage job has no needs; each subsequent needs the previous stage job
    assert "needs" not in jobs["test"]
    assert jobs["build"]["needs"] == "test"
    assert jobs["deploy-dev"]["needs"] == "build"
    assert jobs["deploy-prod"]["needs"] == "deploy-dev"


def test_disabled_stage_produces_no_job_and_chain_closes() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [
                {"id": "test", "runs": "t"},
                {"id": "build", "runs": "b", "enabled": False},
                {"id": "deploy", "runs": "d"},
            ],
        }
    )
    wf = _workflow(ir)
    jobs = wf["jobs"]
    assert "build" not in jobs
    # deploy chains over the disabled build straight onto test
    assert jobs["deploy"]["needs"] == "test"


# ===========================================================================
# Manual gates / environments / approvals
# ===========================================================================
def test_environment_key_present_for_env_stage() -> None:
    wf = _workflow(_full_pipeline())
    jobs = wf["jobs"]
    assert jobs["deploy-dev"]["environment"] == "dev"
    assert jobs["deploy-prod"]["environment"] == "prod"


def test_manual_stage_without_env_still_gated() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [
                {"id": "test", "runs": "t"},
                {"id": "release", "runs": "r", "when": "manual"},
            ],
        }
    )
    jobs = _workflow(ir)["jobs"]
    # manual stage references an environment so a reviewer must approve
    assert jobs["release"]["environment"] == "release"


# ===========================================================================
# Timeouts
# ===========================================================================
def test_timeout_minutes_mapped() -> None:
    jobs = _workflow(_full_pipeline())["jobs"]
    assert jobs["test"]["timeout-minutes"] == 15
    assert jobs["build"]["timeout-minutes"] == 30
    # a stage with no timeout omits the key
    assert "timeout-minutes" not in jobs["deploy-dev"]


# ===========================================================================
# Security / scans
# ===========================================================================
def test_security_job_present_with_requested_scans() -> None:
    jobs = _workflow(_full_pipeline())["jobs"]
    assert "security" in jobs
    step_names = [s.get("name") for s in jobs["security"]["steps"]]
    assert "SAST scan" in step_names
    assert "Secret scan" in step_names
    # dependency was not requested -> not present
    assert "Dependency scan" not in step_names


def test_no_security_job_when_no_scans() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [{"id": "test", "runs": "t"}],
        }
    )
    jobs = _workflow(ir)["jobs"]
    assert "security" not in jobs


# ===========================================================================
# Cloud OIDC
# ===========================================================================
def test_oidc_permissions_and_aws_step() -> None:
    wf = _workflow(_full_pipeline())
    assert wf["permissions"]["id-token"] == "write"
    # the aws credentials step appears in a stage job
    steps = wf["jobs"]["deploy-dev"]["steps"]
    uses = [s.get("uses") for s in steps]
    assert any(u and "aws-actions/configure-aws-credentials" in u for u in uses)


def test_oidc_azure_login_step() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "azure-functions",
            "project": {"name": "demo"},
            "cloud": {"provider": "azure", "auth": "oidc"},
            "stages": [{"id": "deploy", "runs": "d"}],
        }
    )
    wf = _workflow(ir)
    assert wf["permissions"]["id-token"] == "write"
    steps = wf["jobs"]["deploy"]["steps"]
    uses = [s.get("uses") for s in steps]
    assert any(u and "azure/login" in u for u in uses)


def test_no_oidc_permission_when_keys_auth() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "cloud": {"provider": "aws", "auth": "keys"},
            "stages": [{"id": "deploy", "runs": "d"}],
        }
    )
    wf = _workflow(ir)
    # Static-key auth: NO id-token permission is needed (keys aren't a minted token)...
    assert "permissions" not in wf
    steps = wf["jobs"]["deploy"]["steps"]
    uses = [s.get("uses") for s in steps]
    # ...but a credentials step IS present, logging in with the access-key secrets
    # (you cannot deploy to AWS without authenticating). Values are references only.
    assert any(u and "configure-aws-credentials" in u for u in uses)
    content = _emit(ir).content
    assert "${{ secrets.AWS_ACCESS_KEY_ID }}" in content
    assert "${{ secrets.AWS_SECRET_ACCESS_KEY }}" in content
    # no OIDC role-to-assume under key auth
    assert "role-to-assume" not in content


# ===========================================================================
# Secrets by reference only (R6.3/R6.6)
# ===========================================================================
def test_secrets_only_by_reference_no_literals() -> None:
    content = _emit(_full_pipeline()).content
    # every secret appears as a ${{ secrets.NAME }} reference
    assert "${{ secrets.AWS_DEPLOY_ROLE_ARN }}" in content
    # the role ARN reference is never a literal arn:aws:iam value
    assert "arn:aws:iam" not in content
    # no obvious literal credential assignments
    assert not re.search(r"(?i)password\s*[:=]\s*\S+", content)


# ===========================================================================
# The `on:` trigger key must survive (not become true:)
# ===========================================================================
def test_on_trigger_key_survives_roundtrip() -> None:
    wf = _workflow(_full_pipeline())
    # PyYAML must read back a string key "on", not boolean True
    assert "on" in wf
    assert True not in wf  # the boolean True key must NOT be present
    assert "push" in wf["on"]
    assert "pull_request" in wf["on"]


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
    wf = _workflow(_full_pipeline())
    assert isinstance(wf, dict)
    assert wf["name"] == "demo CI"
    assert isinstance(wf["jobs"], dict) and wf["jobs"]
