"""Unit tests for the AWS SageMaker target-platform plugin (task 23.6, R4.2/R7.1, §11.9).

Loads the real ``plugins/target/aws/sagemaker`` package via the loader (no mocks),
builds a :class:`VerbContext` from a representative aws-sagemaker :class:`Pipeline`,
calls each verb, and asserts the design §11.9 aws-sagemaker mapping (container path):

    * build   -> ``docker build`` + emits a resilient Dockerfile (pinned base, NO
                 fragile apt-mirror step) (R7.1);
    * package -> ECR login + ``docker push`` (immutable digest on promotion);
    * deploy  -> create/update SageMaker Model + EndpointConfig + Endpoint;
    * verify  -> poll the endpoint status to InService;
    * lint/test are reasonable/generic.

Plus the IaC contract and the cross-cutting guarantees — secrets by reference only
(R6), deterministic output (R5.3), the container path DECLARES container_images.
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
from cicdgen.plugins.base import Capability

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"

_PROMO_KEY = "promotion_artifact_ref"


def _plugin():
    return load_target_platform_plugin("aws-sagemaker", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-sagemaker",
            "project": {"name": "demo-ml"},
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
# Loader / identity / capabilities
# ===========================================================================
def test_plugin_loads_by_name() -> None:
    assert _plugin().name == "aws-sagemaker"


def test_all_six_verbs_dispatch_via_run_verb() -> None:
    plugin = _plugin()
    for verb in StageVerb:
        assert plugin.run_verb(_ctx(verb)).commands, f"verb {verb} produced no commands"


def test_capabilities_declare_container_images_and_promotion() -> None:
    caps = _plugin().capabilities
    # container path (bring-your-own-container): an image IS built.
    assert Capability.CONTAINER_IMAGES in caps
    assert Capability.ARTIFACT_PROMOTION in caps
    assert Capability.OIDC in caps
    assert Capability.ENVIRONMENTS in caps
    assert Capability.SECRETS_BY_REFERENCE in caps


# ===========================================================================
# build -> docker build + resilient Dockerfile
# ===========================================================================
def test_build_runs_docker_build_and_emits_dockerfile() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    text = _joined(result)
    assert "docker build" in text
    paths = [f.path for f in result.files]
    assert "Dockerfile" in paths


def test_dockerfile_is_resilient_no_apt_mirror() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    dockerfile = next(f for f in result.files if f.path == "Dockerfile").content
    low = dockerfile.lower()
    assert "python:3.12-slim" in dockerfile
    assert "apt-get update" not in low
    assert "apt-get install" not in low


# ===========================================================================
# package -> ECR push, immutable digest on promotion
# ===========================================================================
def test_package_pushes_image_to_ecr() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "docker push" in text
    assert "aws ecr get-login-password" in text


def test_package_without_promotion_does_not_resolve_a_digest() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "describe-images" not in text
    assert "imageDigest" not in text


def test_package_with_promotion_exports_immutable_digest_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    assert "aws ecr describe-images" in text
    assert "imageDigest" in text
    assert "export PROMOTION_ARTIFACT_REF=" in text


# ===========================================================================
# deploy -> create/update model + endpoint config + endpoint
# ===========================================================================
def test_deploy_creates_model_endpoint_config_and_endpoint() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "aws sagemaker create-model" in text
    assert "aws sagemaker create-endpoint-config" in text
    assert "aws sagemaker create-endpoint" in text
    assert "aws sagemaker update-endpoint" in text


def test_deploy_with_promotion_references_prebuilt_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "$PROMOTION_ARTIFACT_REF" in text
    assert "docker build" not in text


# ===========================================================================
# verify -> poll endpoint status to InService
# ===========================================================================
def test_verify_polls_endpoint_status_to_inservice() -> None:
    text = _joined(_plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod")))
    assert "aws sagemaker describe-endpoint" in text
    assert "InService" in text


# ===========================================================================
# lint / test
# ===========================================================================
def test_lint_targets_the_dockerfile() -> None:
    text = _joined(_plugin().lint(_ctx(StageVerb.LINT))).lower()
    assert "dockerfile" in text


def test_test_honours_stage_runs_when_set() -> None:
    result = _plugin().test(_ctx(StageVerb.TEST, stage_id="test", runs="pytest -q"))
    assert "pytest -q" in _joined(result)


def test_test_falls_back_to_placeholder_when_runs_unset() -> None:
    pipeline = _pipeline()
    stage = pipeline.stages[0].model_copy(update={"runs": None})
    ctx = VerbContext(pipeline=pipeline, stage=stage, verb=StageVerb.TEST, settings={})
    assert _plugin().test(ctx).commands


# ===========================================================================
# settings override non-secret knobs
# ===========================================================================
def test_settings_inline_non_secret_knobs() -> None:
    settings = {
        "ecr_repo": "123.dkr.ecr.eu-west-2.amazonaws.com/demo",
        "image_tag": "v1",
        "endpoint_name": "demo-endpoint",
    }
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    assert "123.dkr.ecr.eu-west-2.amazonaws.com/demo:v1" in pkg
    verify = _joined(
        _plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod", settings=settings))
    )
    assert "demo-endpoint" in verify


# ===========================================================================
# supply_iac -> Terraform mentioning SageMaker / ECR / S3 / IAM / OIDC + region
# ===========================================================================
def test_supply_iac_returns_terraform_files_under_platform_dir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    assert paths and all(p.startswith("iac/aws-sagemaker/") for p in paths)


def test_iac_mentions_sagemaker_ecr_s3_iam_and_oidc() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "aws_sagemaker_model" in blob
    assert "aws_sagemaker_endpoint" in blob
    assert "aws_ecr_repository" in blob
    assert "aws_s3_bucket" in blob
    assert "aws_iam_role" in blob
    assert "aws_iam_openid_connect_provider" in blob


def test_iac_references_region_from_ir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "eu-west-2" in blob


def test_iac_summary_describes_what_it_provisions() -> None:
    summary = _plugin().supply_iac(_pipeline()).summary.lower()
    assert "sagemaker" in summary
    assert "oidc" in summary or "iam" in summary


# ===========================================================================
# Secrets by reference only (R6)
# ===========================================================================
def _all_generated_text(plugin) -> str:
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
    blob = _all_generated_text(_plugin())
    assert not re.search(r"arn:aws:iam::\d{12}:(role|user)/", blob)
    assert not re.search(r"AKIA[0-9A-Z]{16}", blob)
    assert not re.search(
        r"(?i)\b(password|secret_key|aws_secret_access_key)\s*[:=]\s*\S+", blob
    )
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "$ECR_REPO" in pkg


# ===========================================================================
# Determinism (R5.3)
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


def test_generated_iac_files_are_generatedfile_instances() -> None:
    iac = _plugin().supply_iac(_pipeline())
    assert all(isinstance(f, GeneratedFile) for f in iac.files)
