"""Unit tests for the GitLab CI-vendor plugin (task 12.1, R3.2/R3.4, design §8a).

Loads the real ``plugins/ci/gitlab`` package via the loader (no mocks, no
stand-in plugin), builds a :class:`StageGraph` from representative IRs, calls
``emit_pipeline_files``, and parses the emitted ``.gitlab-ci.yml`` with PyYAML to
assert the design §8a GitLab CI mapping holds:

    * a top-level ``stages:`` list (the authoritative order), with a leading
      ``security`` stage when scans are requested;
    * one job per *enabled* stage, keyed by name, with the correct ``stage:`` and
      ``script:`` (``stage.runs`` verbatim);
    * a disabled stage produces no job (and drops out of the stages list);
    * ``when: manual`` for a manual stage / a manual-gated environment;
    * ``environment: {name: …}`` for env-targeted stages;
    * ``timeout:`` set from ``stage.timeout_minutes``;
    * a security job per requested scan when scans are asked;
    * ``id_tokens:`` + a provider assume-role step under cloud OIDC (aws/azure);
    * secrets appear only as ``$NAME`` references — no literal values;
    * output is deterministic — two calls yield byte-identical content.

The emitted YAML is also validated as *parseable* GitLab CI structure.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from cicdgen.ir import Pipeline
from cicdgen.plugins import GeneratedFile, StageGraph, load_ci_vendor_plugin

# plugins/ live at <repo>/plugins; this test file is <repo>/tests/test_plugin_gitlab.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_ci_vendor_plugin("gitlab", plugins_root=PLUGINS_ROOT)


def _emit(pipeline: Pipeline) -> GeneratedFile:
    graph = StageGraph.from_pipeline(pipeline)
    files = _plugin().emit_pipeline_files(graph)
    assert len(files) == 1
    return files[0]


def _doc(pipeline: Pipeline) -> dict:
    content = _emit(pipeline).content
    return yaml.safe_load(content)


def _full_pipeline() -> Pipeline:
    """A representative IR exercising order, gates, timeout, scans, and OIDC."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "gitlab",
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
    assert plugin.name == "gitlab"


def test_emits_single_pipeline_file() -> None:
    f = _emit(_full_pipeline())
    assert f.path == ".gitlab-ci.yml"
    assert not f.executable


# ===========================================================================
# Stage order via top-level stages: list + each job's stage:
# ===========================================================================
def test_top_level_stages_list_present_and_ordered() -> None:
    doc = _doc(_full_pipeline())
    assert isinstance(doc["stages"], list)
    # security heads the list (scans requested), then declared order
    assert doc["stages"] == ["security", "test", "build", "deploy-dev", "deploy-prod"]


def test_jobs_exist_for_enabled_stages_with_correct_stage() -> None:
    doc = _doc(_full_pipeline())
    for jid in ("test", "build", "deploy-dev", "deploy-prod"):
        assert jid in doc, f"missing job {jid}"
        assert doc[jid]["stage"] == jid


def test_job_script_is_stage_runs_verbatim() -> None:
    doc = _doc(_full_pipeline())
    # the last script line is the stage command (OIDC steps may precede it)
    assert doc["build"]["script"][-1] == "make build"
    assert doc["test"]["script"][-1] == "make test"


def test_disabled_stage_produces_no_job_and_drops_from_list() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "gitlab",
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
    assert "build" not in doc
    assert doc["stages"] == ["test", "deploy"]


# ===========================================================================
# Manual gates / environments / approvals
# ===========================================================================
def test_env_stage_has_environment_reference() -> None:
    doc = _doc(_full_pipeline())
    assert doc["deploy-dev"]["environment"] == {"name": "dev"}
    assert doc["deploy-prod"]["environment"] == {"name": "prod"}


def test_manual_gated_environment_sets_when_manual() -> None:
    doc = _doc(_full_pipeline())
    # prod environment has gate=manual -> the deploy-prod job is when: manual
    assert doc["deploy-prod"]["when"] == "manual"
    # dev is auto -> no manual gate
    assert "when" not in doc["deploy-dev"]


def test_manual_stage_without_env_still_gated() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "gitlab",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [
                {"id": "test", "runs": "t"},
                {"id": "release", "runs": "r", "when": "manual"},
            ],
        }
    )
    doc = _doc(ir)
    assert doc["release"]["when"] == "manual"
    # references an environment so a protected-environment approval can gate it
    assert doc["release"]["environment"] == {"name": "release"}


# ===========================================================================
# Timeouts
# ===========================================================================
def test_timeout_mapped() -> None:
    doc = _doc(_full_pipeline())
    assert doc["test"]["timeout"] == "15 minutes"
    assert doc["build"]["timeout"] == "30 minutes"
    # a stage with no timeout omits the key
    assert "timeout" not in doc["deploy-dev"]


# ===========================================================================
# Security / scans
# ===========================================================================
def test_security_jobs_present_for_requested_scans() -> None:
    doc = _doc(_full_pipeline())
    # sast + secrets requested -> their jobs exist in the security stage
    assert doc["sast"]["stage"] == "security"
    assert doc["secret_detection"]["stage"] == "security"
    # dependency was not requested -> not present
    assert "dependency_scanning" not in doc


def test_no_security_stage_when_no_scans() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "gitlab",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [{"id": "test", "runs": "t"}],
        }
    )
    doc = _doc(ir)
    assert "security" not in doc["stages"]
    assert "sast" not in doc


# ===========================================================================
# Cloud OIDC (id_tokens + assume role)
# ===========================================================================
def test_oidc_id_tokens_and_aws_assume_role() -> None:
    doc = _doc(_full_pipeline())
    job = doc["deploy-dev"]
    # id_tokens requested with the AWS audience
    assert job["id_tokens"]["OIDC_TOKEN"]["aud"] == "sts.amazonaws.com"
    # an assume-role step references the role ARN variable (not a literal)
    script = "\n".join(job["script"])
    assert "assume-role-with-web-identity" in script
    assert "$AWS_DEPLOY_ROLE_ARN" in script


def test_oidc_azure_login_step() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "gitlab",
            "target_platform": "azure-functions",
            "project": {"name": "demo"},
            "cloud": {"provider": "azure", "auth": "oidc"},
            "stages": [{"id": "deploy", "runs": "d"}],
        }
    )
    doc = _doc(ir)
    job = doc["deploy"]
    assert job["id_tokens"]["OIDC_TOKEN"]["aud"] == "api://AzureADTokenExchange"
    script = "\n".join(job["script"])
    assert "az login" in script
    assert "$AZURE_CLIENT_ID" in script


def test_no_oidc_when_keys_auth() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "gitlab",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "cloud": {"provider": "aws", "auth": "keys"},
            "stages": [{"id": "deploy", "runs": "d"}],
        }
    )
    doc = _doc(ir)
    job = doc["deploy"]
    assert "id_tokens" not in job
    assert "assume-role-with-web-identity" not in "\n".join(job["script"])


# ===========================================================================
# Retries (GitLab native)
# ===========================================================================
def test_retries_map_to_native_retry() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "gitlab",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [{"id": "test", "runs": "t", "retries": 2}],
        }
    )
    doc = _doc(ir)
    assert doc["test"]["retry"] == 2


# ===========================================================================
# Secrets by reference only (R6.3/R6.6)
# ===========================================================================
def test_secrets_only_by_reference_no_literals() -> None:
    content = _emit(_full_pipeline()).content
    # the role ARN is referenced as a $VAR, never a literal arn:aws:iam value
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
    assert isinstance(doc["stages"], list) and doc["stages"]
    # at least one job mapping besides the stages list
    assert isinstance(doc["build"], dict)
