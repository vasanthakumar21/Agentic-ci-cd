"""Unit tests for the AWS Step Functions target-platform plugin (task 23.6, §11.9).

Loads the real ``plugins/target/aws/stepfunctions`` package via the loader (no
mocks), builds a :class:`VerbContext` from a representative aws-stepfunctions
:class:`Pipeline`, calls each verb, and asserts the design §11.9 mapping:

    * build   -> assemble/validate the ASL state-machine definition (NO image);
    * package -> upload the definition to S3 (immutable object VERSION as the ref);
    * deploy  -> ``aws stepfunctions create-state-machine`` / ``update-state-machine``;
    * verify  -> ``aws stepfunctions start-execution`` + poll status to SUCCEEDED;
    * lint/test are reasonable/generic (no Dockerfile lint).

Plus the IaC contract and the cross-cutting guarantees — secrets by reference only
(R6), deterministic output (R5.3), the orchestration path declares no
container_images.
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
    return load_target_platform_plugin("aws-stepfunctions", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-stepfunctions",
            "project": {"name": "demo-flow"},
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
    assert _plugin().name == "aws-stepfunctions"


def test_all_six_verbs_dispatch_via_run_verb() -> None:
    plugin = _plugin()
    for verb in StageVerb:
        assert plugin.run_verb(_ctx(verb)).commands, f"verb {verb} produced no commands"


def test_capabilities_declare_promotion_oidc_but_not_container_images() -> None:
    caps = _plugin().capabilities
    assert Capability.ARTIFACT_PROMOTION in caps
    assert Capability.OIDC in caps
    assert Capability.ENVIRONMENTS in caps
    assert Capability.SECRETS_BY_REFERENCE in caps
    assert Capability.CONTAINER_IMAGES not in caps


# ===========================================================================
# build -> ASL definition, NO image
# ===========================================================================
def test_build_emits_asl_definition_and_not_an_image() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    text = _joined(result)
    paths = [f.path for f in result.files]
    assert "stepfunctions/state-machine.asl.json" in paths
    asl = next(
        f for f in result.files if f.path == "stepfunctions/state-machine.asl.json"
    ).content
    assert "StartAt" in asl
    assert "States" in asl
    assert "docker build" not in text
    assert "docker push" not in text
    assert "docker" not in asl.lower()


# ===========================================================================
# package -> upload definition to S3, capture immutable object version
# ===========================================================================
def test_package_uploads_definition_to_s3() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "aws s3api put-object" in text
    assert "docker push" not in text


def test_package_without_promotion_does_not_resolve_a_version() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "head-object" not in text
    assert "versionId=" not in text


def test_package_with_promotion_exports_immutable_version_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    assert "head-object" in text
    assert "VersionId" in text
    assert "export PROMOTION_ARTIFACT_REF=" in text
    assert "versionId=$OBJECT_VERSION" in text


# ===========================================================================
# deploy -> create/update state machine
# ===========================================================================
def test_deploy_creates_or_updates_the_state_machine() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "aws stepfunctions create-state-machine" in text
    assert "aws stepfunctions update-state-machine" in text


def test_deploy_with_promotion_references_prebuilt_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "$PROMOTION_ARTIFACT_REF" in text
    assert "aws s3api put-object" not in text


# ===========================================================================
# verify -> start execution + poll to SUCCEEDED
# ===========================================================================
def test_verify_starts_execution_and_polls_status() -> None:
    text = _joined(_plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod")))
    assert "aws stepfunctions start-execution" in text
    assert "aws stepfunctions describe-execution" in text
    assert "SUCCEEDED" in text


# ===========================================================================
# lint / test are reasonable and generic (no Dockerfile lint)
# ===========================================================================
def test_lint_targets_asl_definition_not_a_dockerfile() -> None:
    text = _joined(_plugin().lint(_ctx(StageVerb.LINT))).lower()
    assert "asl" in text or "definition" in text or "json" in text
    assert "dockerfile" not in text


def test_test_honours_stage_runs_when_set() -> None:
    result = _plugin().test(_ctx(StageVerb.TEST, stage_id="test", runs="pytest -q"))
    assert "pytest -q" in _joined(result)


def test_test_falls_back_to_placeholder_when_runs_unset() -> None:
    pipeline = _pipeline()
    stage = pipeline.stages[0].model_copy(update={"runs": None})
    ctx = VerbContext(pipeline=pipeline, stage=stage, verb=StageVerb.TEST, settings={})
    assert _plugin().test(ctx).commands


# ===========================================================================
# settings override non-secret knobs (value instead of $REF)
# ===========================================================================
def test_settings_inline_non_secret_knobs() -> None:
    settings = {
        "state_machine_name": "demo-sm",
        "artifact_bucket": "demo-bucket",
        "definition_key": "stepfunctions/demo.asl.json",
    }
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    assert "demo-bucket" in pkg
    assert "stepfunctions/demo.asl.json" in pkg
    deploy = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "demo-sm" in deploy


# ===========================================================================
# supply_iac -> Terraform mentioning SFN / IAM / S3 / OIDC and the region
# ===========================================================================
def test_supply_iac_returns_terraform_files_under_platform_dir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    assert paths and all(p.startswith("iac/aws-stepfunctions/") for p in paths)


def test_iac_mentions_state_machine_iam_and_oidc() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "aws_sfn_state_machine" in blob
    assert "aws_iam_role" in blob
    assert "aws_iam_openid_connect_provider" in blob


def test_iac_references_region_from_ir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "eu-west-2" in blob


def test_iac_summary_describes_what_it_provisions() -> None:
    summary = _plugin().supply_iac(_pipeline()).summary.lower()
    assert "step functions" in summary or "state machine" in summary
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
    assert "$SFN_ARTIFACT_BUCKET" in pkg


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
