"""Unit tests for the AWS ECS target-platform plugin (task 8.1, R4.2/R7.1, §8b).

Loads the real ``plugins/target/aws-ecs`` package via the loader (no mocks, no
stand-in plugin), builds a :class:`VerbContext` from a representative aws-ecs
:class:`Pipeline`, calls each verb, and asserts the design §8b aws-ecs mapping:

    * build   -> ``docker build`` + emits a resilient Dockerfile (pinned base,
                 NO fragile apt-mirror step) (R7.1);
    * package -> ECR login + ``docker push``;
    * deploy  -> ``aws ecs register-task-definition`` + ``update-service``;
    * verify  -> an ECS health/stability check;
    * lint/test are reasonable/generic.

Plus the IaC contract: :meth:`supply_iac` returns Terraform mentioning ECR/ECS/IAM
and the region; and the cross-cutting guarantees — secrets by reference only (R6)
and deterministic output (R5.3).
"""

from __future__ import annotations

import re
from pathlib import Path

from cicdgen.ir import Pipeline
from cicdgen.plugins import (
    GeneratedFile,
    StageVerb,
    VerbContext,
    load_target_platform_plugin,
)

# plugins/ live at <repo>/plugins; this test file is <repo>/tests/test_plugin_aws_ecs.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_target_platform_plugin("aws-ecs", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    """A representative aws-ecs IR (GitHub + AWS ECS, OIDC)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-ecs",
            "project": {"name": "demo-svc"},
            "cloud": {"provider": "aws", "region": "eu-west-2", "auth": "oidc"},
            "environments": [{"name": "prod", "gate": "manual"}],
            "stages": [
                {"id": "test", "runs": "pytest -q"},
                {"id": "build", "runs": "make build"},
                {"id": "deploy-prod", "runs": "make deploy", "environment": "prod"},
            ],
        }
    )


def _ctx(verb: StageVerb, *, stage_id: str = "build", runs: str | None = None,
         settings: dict | None = None) -> VerbContext:
    pipeline = _pipeline()
    stage = next((s for s in pipeline.stages if s.id == stage_id), pipeline.stages[0])
    if runs is not None:
        stage = stage.model_copy(update={"runs": runs})
    return VerbContext(
        pipeline=pipeline, stage=stage, verb=verb, settings=settings or {}
    )


def _joined(result) -> str:
    return "\n".join(result.commands)


# ===========================================================================
# Loader / identity
# ===========================================================================
def test_plugin_loads_by_name() -> None:
    plugin = _plugin()
    assert plugin.name == "aws-ecs"


def test_all_six_verbs_dispatch_via_run_verb() -> None:
    plugin = _plugin()
    for verb in StageVerb:
        result = plugin.run_verb(_ctx(verb))
        # every verb returns a VerbResult with at least one command
        assert result.commands, f"verb {verb} produced no commands"


# ===========================================================================
# build -> docker build + resilient Dockerfile (R7.1)
# ===========================================================================
def test_build_runs_docker_build() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    text = _joined(result)
    assert "docker build" in text


def test_build_emits_dockerfile_support_file() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    paths = [f.path for f in result.files]
    assert "Dockerfile" in paths


def test_dockerfile_pins_base_image() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    dockerfile = next(f for f in result.files if f.path == "Dockerfile")
    # pinned to a specific minor, never :latest or a bare major
    assert "python:3.12-slim" in dockerfile.content
    assert ":latest" not in dockerfile.content


def test_dockerfile_has_no_fragile_apt_mirror_step() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    dockerfile = next(f for f in result.files if f.path == "Dockerfile")
    content = dockerfile.content.lower()
    # the resilience requirement: no live apt-mirror dependency (R7.1)
    assert "apt-get update" not in content
    assert "apt-get install" not in content
    assert "apt install" not in content
    # it installs deps via pip from requirements instead
    assert "pip install" in dockerfile.content
    assert "requirements.txt" in dockerfile.content


def test_dockerfile_is_multistage() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    dockerfile = next(f for f in result.files if f.path == "Dockerfile")
    # two FROM ... AS <stage> lines -> multi-stage
    stages = re.findall(r"(?mi)^FROM\s+\S+\s+AS\s+\w+", dockerfile.content)
    assert len(stages) >= 2


# ===========================================================================
# package -> ECR login + docker push
# ===========================================================================
def test_package_logs_into_ecr_and_pushes() -> None:
    result = _plugin().package(_ctx(StageVerb.PACKAGE))
    text = _joined(result)
    assert "aws ecr get-login-password" in text
    assert "docker login" in text
    assert "docker push" in text


# ===========================================================================
# deploy -> register task def + update service
# ===========================================================================
def test_deploy_registers_taskdef_and_updates_service() -> None:
    result = _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod"))
    text = _joined(result)
    assert "aws ecs register-task-definition" in text
    assert "aws ecs update-service" in text


# ===========================================================================
# verify -> ECS health check
# ===========================================================================
def test_verify_checks_ecs_service_health() -> None:
    result = _plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod"))
    text = _joined(result)
    # a stability wait and/or a describe-services health read
    assert "aws ecs wait services-stable" in text or "aws ecs describe-services" in text


# ===========================================================================
# lint / test are reasonable and generic
# ===========================================================================
def test_lint_targets_the_dockerfile() -> None:
    result = _plugin().lint(_ctx(StageVerb.LINT))
    text = _joined(result).lower()
    assert "dockerfile" in text


def test_test_honours_stage_runs_when_set() -> None:
    result = _plugin().test(_ctx(StageVerb.TEST, stage_id="test", runs="pytest -q"))
    assert "pytest -q" in _joined(result)


def test_test_falls_back_to_placeholder_when_runs_unset() -> None:
    # a stage with runs=None -> generic placeholder, not a crash
    pipeline = _pipeline()
    stage = pipeline.stages[0].model_copy(update={"runs": None})
    ctx = VerbContext(pipeline=pipeline, stage=stage, verb=StageVerb.TEST, settings={})
    result = _plugin().test(ctx)
    assert result.commands  # a non-empty placeholder command


# ===========================================================================
# settings override non-secret knobs (value instead of $REF)
# ===========================================================================
def test_settings_inline_non_secret_knobs() -> None:
    settings = {
        "ecr_repo": "123456789012.dkr.ecr.eu-west-2.amazonaws.com/demo-svc",
        "image_tag": "abc123",
        "ecs_cluster": "demo-cluster",
        "ecs_service": "demo-service",
    }
    build = _plugin().build(_ctx(StageVerb.BUILD, settings=settings))
    assert "demo-svc:abc123" in _joined(build)
    deploy = _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    text = _joined(deploy)
    assert "demo-cluster" in text
    assert "demo-service" in text


# ===========================================================================
# supply_iac -> Terraform mentioning ECR / ECS / IAM and the region
# ===========================================================================
def test_supply_iac_returns_terraform_files() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    # all Terraform lives under the per-platform iac dir
    assert paths and all(p.startswith("iac/aws-ecs/") for p in paths)


def test_iac_mentions_ecr_ecs_iam_and_oidc() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "aws_ecr_repository" in blob
    assert "aws_ecs_cluster" in blob
    assert "aws_ecs_service" in blob
    assert "aws_ecs_task_definition" in blob
    assert "aws_iam_role" in blob
    assert "aws_iam_openid_connect_provider" in blob


def test_iac_references_region_from_ir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    # the IR region surfaces (as the aws_region default / tfvars example)
    assert "eu-west-2" in blob


def test_iac_summary_describes_what_it_provisions() -> None:
    iac = _plugin().supply_iac(_pipeline())
    summary = iac.summary.lower()
    assert "ecr" in summary
    assert "ecs" in summary
    assert "oidc" in summary or "iam" in summary


# ===========================================================================
# Secrets by reference only (R6) — no literal account specifics / secrets
# ===========================================================================
def _all_generated_text(plugin) -> str:
    """Every command + file across all verbs + IaC, concatenated."""
    parts: list[str] = []
    for verb in StageVerb:
        for stage_id in ("test", "build", "deploy-prod"):
            result = plugin.run_verb(_ctx(verb, stage_id=stage_id))
            parts.extend(result.commands)
            parts.extend(f.content for f in result.files)
    iac = plugin.supply_iac(_pipeline())
    parts.extend(f.content for f in iac.files)
    parts.append(iac.summary)
    return "\n".join(parts)


def test_no_literal_secrets_or_account_specifics_without_settings() -> None:
    # Build everything WITHOUT inlining settings -> account specifics must be refs.
    blob = _all_generated_text(_plugin())
    # no literal IAM ARNs committed (the only arn: strings are AWS-managed policy
    # arns / service principals in TF, which are not account specifics or secrets)
    assert "arn:aws:iam::0" not in blob
    assert not re.search(r"arn:aws:iam::\d{12}:(role|user)/", blob)
    # no AWS access key id shapes
    assert not re.search(r"AKIA[0-9A-Z]{16}", blob)
    # no obvious literal secret/password assignments
    assert not re.search(r"(?i)\b(password|secret_key|aws_secret_access_key)\s*[:=]\s*\S+", blob)
    # the ECR repo / region are referenced by shell var when not set via settings
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "$ECR_REPO" in pkg


# ===========================================================================
# Determinism (R5.3) — two calls / two fresh instances are byte-identical
# ===========================================================================
def test_verbs_are_deterministic_across_calls() -> None:
    plugin = _plugin()
    for verb in StageVerb:
        a = plugin.run_verb(_ctx(verb, stage_id="deploy-prod"))
        b = plugin.run_verb(_ctx(verb, stage_id="deploy-prod"))
        assert a.commands == b.commands
        assert tuple((f.path, f.content) for f in a.files) == \
            tuple((f.path, f.content) for f in b.files)


def test_iac_is_deterministic_across_fresh_instances() -> None:
    a = _plugin().supply_iac(_pipeline())
    b = _plugin().supply_iac(_pipeline())
    assert a.summary == b.summary
    assert tuple((f.path, f.content) for f in a.files) == \
        tuple((f.path, f.content) for f in b.files)


# ===========================================================================
# GeneratedFile sanity
# ===========================================================================
def test_generated_files_are_generatedfile_instances() -> None:
    build = _plugin().build(_ctx(StageVerb.BUILD))
    assert all(isinstance(f, GeneratedFile) for f in build.files)
    iac = _plugin().supply_iac(_pipeline())
    assert all(isinstance(f, GeneratedFile) for f in iac.files)
