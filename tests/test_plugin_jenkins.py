"""Unit tests for the Jenkins CI-vendor plugin (task 12.1, R3.4, design §8a).

Loads the real ``plugins/ci/jenkins`` package via the loader (no mocks, no
stand-in plugin), builds a :class:`StageGraph` from representative IRs, calls
``emit_pipeline_files``, and asserts the design §8a Jenkins mapping holds.

A ``Jenkinsfile`` is **Groovy, not YAML**, so (unlike the GitHub / Azure tests)
these assertions are over the emitted *text* / structure rather than a parsed
document:

    * the file is a declarative pipeline — starts with ``pipeline {``, has a
      ``stages {`` block;
    * one ``stage('id')`` per enabled stage, in declared order;
    * a manual stage yields an ``input`` step (``input submitter:`` when the stage
      declares an approver group);
    * ``timeout(time: N, unit: 'MINUTES')`` wraps timed stages;
    * a ``security`` stage with a scan step per requested scan;
    * secrets appear only as ``credentials('NAME')`` references — no literal
      secret values;
    * output is deterministic — two emits are byte-identical.
"""

from __future__ import annotations

import re
from pathlib import Path

from cicdgen.ir import Pipeline
from cicdgen.plugins import GeneratedFile, StageGraph, load_ci_vendor_plugin

# plugins/ live at <repo>/plugins; this test file is <repo>/tests/test_plugin_jenkins.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_ci_vendor_plugin("jenkins", plugins_root=PLUGINS_ROOT)


def _emit(pipeline: Pipeline) -> GeneratedFile:
    graph = StageGraph.from_pipeline(pipeline)
    files = _plugin().emit_pipeline_files(graph)
    assert len(files) == 1
    return files[0]


def _content(pipeline: Pipeline) -> str:
    return _emit(pipeline).content


def _full_pipeline() -> Pipeline:
    """A representative IR: manual prod stage, scans, OIDC, timeouts, approvals."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "jenkins",
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
                 "when": "manual",
                 "approvals": {"required": 2, "from_group": "leads"}},
            ],
        }
    )


def _stage_index(content: str, stage_id: str) -> int:
    """Index of the ``stage('id')`` header in the text (-1 if absent)."""
    return content.find(f"stage('{stage_id}')")


# ===========================================================================
# Loader / identity
# ===========================================================================
def test_plugin_loads_by_name() -> None:
    assert _plugin().name == "jenkins"


def test_emits_single_jenkinsfile() -> None:
    f = _emit(_full_pipeline())
    assert f.path == "Jenkinsfile"
    assert not f.executable


# ===========================================================================
# Declarative pipeline skeleton
# ===========================================================================
def test_starts_with_declarative_pipeline_block() -> None:
    content = _content(_full_pipeline())
    assert content.startswith("pipeline {")
    assert "agent any" in content
    assert "stages {" in content


def test_pipeline_block_is_balanced() -> None:
    # A well-formed Groovy block has matching braces.
    content = _content(_full_pipeline())
    assert content.count("{") == content.count("}")


# ===========================================================================
# Stage order
# ===========================================================================
def test_stage_blocks_exist_for_enabled_stages() -> None:
    content = _content(_full_pipeline())
    for sid in ("test", "build", "deploy-dev", "deploy-prod"):
        assert f"stage('{sid}')" in content, f"missing stage {sid}"


def test_stages_appear_in_declared_order() -> None:
    content = _content(_full_pipeline())
    order = [_stage_index(content, s) for s in ("test", "build", "deploy-dev", "deploy-prod")]
    assert all(i >= 0 for i in order)
    assert order == sorted(order), "stages must appear in declared order"


def test_disabled_stage_produces_no_block() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "jenkins",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [
                {"id": "test", "runs": "t"},
                {"id": "build", "runs": "b", "enabled": False},
                {"id": "deploy", "runs": "d"},
            ],
        }
    )
    content = _content(ir)
    assert "stage('build')" not in content
    assert "stage('test')" in content
    assert "stage('deploy')" in content


def test_run_command_taken_verbatim_from_ir() -> None:
    content = _content(_full_pipeline())
    assert "sh 'make test'" in content
    assert "sh 'make build'" in content
    assert "sh 'make deploy'" in content


# ===========================================================================
# Manual gates / approvals
# ===========================================================================
def test_manual_stage_has_input_step() -> None:
    content = _content(_full_pipeline())
    # deploy-prod is when=manual AND targets a manual-gated env -> input step
    prod = content[_stage_index(content, "deploy-prod"):]
    assert "input message: 'Approve deploy-prod?'" in prod


def test_input_uses_submitter_for_approver_group() -> None:
    content = _content(_full_pipeline())
    assert "submitter: 'leads'" in content


def test_manual_gated_environment_triggers_input_without_when() -> None:
    # deploy-dev is auto (no input); a manual-gated env stage WITHOUT when=manual
    # should still be gated via the environment's gate=manual.
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "jenkins",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "environments": [{"name": "prod", "gate": "manual"}],
            "stages": [
                {"id": "build", "runs": "make build"},
                {"id": "deploy", "runs": "make deploy", "environment": "prod"},
            ],
        }
    )
    content = _content(ir)
    build = content[_stage_index(content, "build"):_stage_index(content, "deploy")]
    deploy = content[_stage_index(content, "deploy"):]
    assert "input" not in build
    assert "input message: 'Approve deploy?'" in deploy


# ===========================================================================
# Timeouts
# ===========================================================================
def test_timeout_wraps_timed_stages() -> None:
    content = _content(_full_pipeline())
    assert "timeout(time: 15, unit: 'MINUTES')" in content
    assert "timeout(time: 30, unit: 'MINUTES')" in content


def test_untimed_stage_has_no_timeout() -> None:
    content = _content(_full_pipeline())
    dev = content[_stage_index(content, "deploy-dev"):_stage_index(content, "deploy-prod")]
    assert "timeout(" not in dev


# ===========================================================================
# Security / scans
# ===========================================================================
def test_security_stage_present_with_requested_scans() -> None:
    content = _content(_full_pipeline())
    assert "stage('security')" in content
    # the scan commands are single-quoted Groovy literals, so the inner quotes of
    # ``echo 'run SAST scan'`` are escaped -> ``sh 'echo \'run SAST scan\''``.
    assert r"sh 'echo \'run SAST scan\''" in content
    assert r"sh 'echo \'run secret scan\''" in content
    # dependency was not requested -> absent
    assert "run dependency scan" not in content


def test_no_security_stage_when_no_scans() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "jenkins",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "stages": [{"id": "test", "runs": "t"}],
        }
    )
    assert "stage('security')" not in _content(ir)


# ===========================================================================
# Cloud OIDC / assume-role
# ===========================================================================
def test_oidc_emits_assume_role_step_referencing_credential() -> None:
    content = _content(_full_pipeline())
    # credential bound by reference in environment {}
    assert "credentials('AWS_DEPLOY_ROLE_ARN')" in content
    # an assume-role login step is emitted (references the env var, not a literal)
    assert "aws sts assume-role-with-web-identity --role-arn $AWS_ROLE_ARN" in content


def test_no_assume_role_when_keys_auth() -> None:
    ir = Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "jenkins",
            "target_platform": "aws-ecs",
            "project": {"name": "demo"},
            "cloud": {"provider": "aws", "auth": "keys"},
            "stages": [{"id": "deploy", "runs": "d"}],
        }
    )
    content = _content(ir)
    assert "assume-role-with-web-identity" not in content
    assert "credentials(" not in content


# ===========================================================================
# Secrets by reference only (R6.3/R6.6)
# ===========================================================================
def test_secrets_only_by_reference_no_literals() -> None:
    content = _content(_full_pipeline())
    # the role is referenced via credentials('NAME') only
    assert "credentials('AWS_DEPLOY_ROLE_ARN')" in content
    # never a literal IAM ARN value
    assert "arn:aws:iam" not in content
    # no obvious literal credential assignments
    assert not re.search(r"(?i)password\s*=\s*'[^']+'", content)


# ===========================================================================
# Determinism (R5.3)
# ===========================================================================
def test_output_is_byte_identical_across_calls() -> None:
    ir = _full_pipeline()
    assert _content(ir) == _content(ir)


def test_output_identical_across_fresh_plugin_instances() -> None:
    ir = _full_pipeline()
    a = _plugin().emit_pipeline_files(StageGraph.from_pipeline(ir))[0].content
    b = _plugin().emit_pipeline_files(StageGraph.from_pipeline(ir))[0].content
    assert a == b
