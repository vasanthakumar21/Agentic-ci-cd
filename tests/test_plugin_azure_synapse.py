"""Unit tests for the Azure Synapse target-platform plugin (task 23.6, R4.2, §11.9).

azure-synapse is a **data-job shape** (like databricks): NO container image, NO ACR,
NO ECS. These tests load the real ``plugins/target/azure/synapse`` package via the
loader (no mocks), call each verb, and assert the design §11.9 azure-synapse mapping:

    * build   -> assemble a wheel (``python -m build``) + stage Spark defs/notebooks,
                 NO image, and emit the committed Spark job definition;
    * package -> upload artifacts to the workspace ADLS (versioned blob = ref);
    * deploy  -> create/update a Synapse Spark job definition via ``az synapse``;
    * verify  -> start + poll the Synapse Spark job run to Succeeded;
    * lint/test are reasonable/generic.

Plus the CRITICAL contrast: NO synapse verb emits ``docker build`` / ``docker push``
or references ACR/ECS; capabilities declare ``artifact_promotion`` but NOT
``container_images``; secrets by reference only (R6); deterministic output (R5.3).
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
    return load_target_platform_plugin("azure-synapse", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    """A representative azure-synapse IR (Azure DevOps + Synapse, no image)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "azure-devops",
            "target_platform": "azure-synapse",
            "project": {"name": "demo-analytics"},
            "cloud": {"provider": "azure", "region": "uksouth", "auth": "oidc"},
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


# ===========================================================================
# Loader / identity / capabilities
# ===========================================================================
def test_plugin_loads_by_name() -> None:
    assert _plugin().name == "azure-synapse"


def test_all_six_verbs_dispatch_via_run_verb() -> None:
    plugin = _plugin()
    for verb in StageVerb:
        result = plugin.run_verb(_ctx(verb))
        assert result.commands, f"verb {verb} produced no commands"


def test_capabilities_declare_promotion_without_container_images() -> None:
    caps = _plugin().capabilities
    assert Capability.ARTIFACT_PROMOTION in caps
    assert Capability.ENVIRONMENTS in caps
    assert Capability.OIDC in caps
    assert Capability.SECRETS_BY_REFERENCE in caps
    # data-job shape: NO container images
    assert Capability.CONTAINER_IMAGES not in caps


# ===========================================================================
# build -> wheel/notebooks, NO image
# ===========================================================================
def test_build_assembles_a_wheel() -> None:
    text = _joined(_plugin().build(_ctx(StageVerb.BUILD)))
    assert "python -m build" in text


def test_build_does_not_emit_a_dockerfile() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    paths = [f.path.lower() for f in result.files]
    assert not any("dockerfile" in p for p in paths)


def test_build_emits_a_spark_job_definition_support_file() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    paths = [f.path for f in result.files]
    assert any(p.endswith("spark-job-definition.json") for p in paths)


# ===========================================================================
# package -> upload to ADLS (not an image push), with promotion ref
# ===========================================================================
def test_package_uploads_to_adls() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "az storage fs file upload" in text
    assert "docker push" not in text


def test_package_with_promotion_exports_versioned_blob_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    assert "versionId" in text
    assert "export PROMOTION_ARTIFACT_REF=" in text


# ===========================================================================
# deploy -> create/update a Synapse Spark job definition via az synapse
# ===========================================================================
def test_deploy_creates_or_updates_a_spark_job_definition() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "az synapse spark-job-definition" in text
    assert "create" in text or "update" in text


# ===========================================================================
# verify -> start the job run + poll to a terminal state
# ===========================================================================
def test_verify_starts_job_and_polls_run_state() -> None:
    text = _joined(_plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod")))
    assert "az synapse spark-job-definition start" in text
    assert "success" in text.lower()


# ===========================================================================
# CRITICAL: no image shape anywhere
# ===========================================================================
def test_no_verb_emits_docker_or_references_acr_ecs() -> None:
    blob = _all_generated_text(_plugin())
    blob = re.sub(r"NO (ACR|ECR|ECS|container image)", "", blob)
    lowered = blob.lower()
    assert "docker build" not in lowered
    assert "docker push" not in lowered
    assert "az acr" not in lowered
    assert not re.search(r"\becr\b", lowered)
    assert not re.search(r"\becs\b", lowered)


# ===========================================================================
# lint / test are reasonable and generic
# ===========================================================================
def test_lint_is_generic_and_not_dockerfile() -> None:
    text = _joined(_plugin().lint(_ctx(StageVerb.LINT))).lower()
    assert "dockerfile" not in text
    assert "lint" in text


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
        "workspace": "demo-ws",
        "spark_pool": "demopool",
        "storage_account": "demolake",
        "filesystem": "artifacts",
        "job_name": "demo-job",
    }
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    assert "demolake" in pkg
    deploy = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "demo-ws" in deploy
    assert "demo-job" in deploy


# ===========================================================================
# supply_iac -> Synapse Terraform mentioning workspace ref + pool + storage + CI
# ===========================================================================
def test_supply_iac_returns_files_under_platform_dir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    assert paths and all(p.startswith("iac/azure-synapse/") for p in paths)


def test_iac_mentions_synapse_spark_pool_storage_and_federation() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "azurerm_synapse_workspace" in blob
    assert "azurerm_synapse_spark_pool" in blob
    assert "azurerm_storage_account" in blob
    assert "azurerm_federated_identity_credential" in blob


def test_iac_shape_has_no_container_image_resources() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "azurerm_container_registry" not in blob
    assert "aws_ecs" not in blob


def test_iac_references_region_from_ir() -> None:
    blob = "\n".join(f.content for f in _plugin().supply_iac(_pipeline()).files)
    assert "uksouth" in blob


def test_iac_summary_describes_what_it_provisions() -> None:
    summary = _plugin().supply_iac(_pipeline()).summary.lower()
    assert "synapse" in summary
    assert "spark" in summary


# ===========================================================================
# Secrets by reference only (R6)
# ===========================================================================
def test_no_literal_secrets_or_account_specifics_without_settings() -> None:
    blob = _all_generated_text(_plugin())
    assert not re.search(
        r"(?i)\b(password|client_secret|account_key)\s*[:=]\s*\S+", blob
    )
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "$SYNAPSE_STORAGE_ACCOUNT" in pkg
    deploy = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "$SYNAPSE_WORKSPACE" in deploy


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


def test_generated_files_are_generatedfile_instances() -> None:
    build = _plugin().build(_ctx(StageVerb.BUILD))
    assert all(isinstance(f, GeneratedFile) for f in build.files)
    iac = _plugin().supply_iac(_pipeline())
    assert all(isinstance(f, GeneratedFile) for f in iac.files)
