"""Unit tests for the Azure Container Apps plugin (task 23.6, R4.2/R7.1, §11.9).

Loads the real ``plugins/target/azure/container-apps`` package via the loader (no
mocks), builds a :class:`VerbContext` from a representative azure-container-apps
:class:`Pipeline`, calls each verb, and asserts the design §11.9
azure-container-apps mapping:

    * build   -> ``docker build`` (+ a resilient, pinned, apt-mirror-free Dockerfile,
                 R7.1) — Container Apps is container-based (managed service);
    * package -> push the image to ACR (immutable DIGEST as the promotable ref);
    * deploy  -> ``az containerapp update --image`` consuming the pre-built image;
    * verify  -> poll the latest revision provisioningState/health;
    * lint/test are reasonable/generic (Dockerfile lint).

Plus the IaC contract: :meth:`supply_iac` returns Terraform mentioning the Container
App + environment + ACR + identity + federated credential and the region; promotion
consumes a pre-built digest reference; and the cross-cutting guarantees — secrets by
reference only (R6), deterministic output (R5.3), the container capability set.
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
    return load_target_platform_plugin("azure-container-apps", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    """A representative azure-container-apps IR (GitHub + Azure, OIDC)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "azure-container-apps",
            "project": {"name": "demo-svc"},
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


# ===========================================================================
# Loader / identity
# ===========================================================================
def test_plugin_loads_by_name() -> None:
    assert _plugin().name == "azure-container-apps"


def test_all_six_verbs_dispatch_via_run_verb() -> None:
    plugin = _plugin()
    for verb in StageVerb:
        result = plugin.run_verb(_ctx(verb))
        assert result.commands, f"verb {verb} produced no commands"


def test_capabilities_declare_container_promotion_and_oidc() -> None:
    caps = _plugin().capabilities
    assert Capability.CONTAINER_IMAGES in caps
    assert Capability.ARTIFACT_PROMOTION in caps
    assert Capability.ENVIRONMENTS in caps
    assert Capability.OIDC in caps
    assert Capability.SECRETS_BY_REFERENCE in caps


# ===========================================================================
# build -> docker build, emits resilient Dockerfile (R7.1)
# ===========================================================================
def test_build_runs_docker_build_and_emits_dockerfile() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    text = _joined(result)
    assert "docker build" in text
    assert "Dockerfile" in [f.path for f in result.files]


def test_build_dockerfile_is_resilient_no_apt_mirror() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    dockerfile = next(f for f in result.files if f.path == "Dockerfile")
    low = dockerfile.content.lower()
    assert "python:3.12-slim" in dockerfile.content
    assert "pip install" in low
    assert "apt-get update" not in low
    assert "apt-get install" not in low


# ===========================================================================
# package -> push to ACR, capture immutable digest (promotion)
# ===========================================================================
def test_package_pushes_to_acr() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "az acr login" in text
    assert "docker push" in text


def test_package_without_promotion_does_not_resolve_a_digest() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "az acr repository show" not in text
    assert "IMAGE_DIGEST" not in text


def test_package_with_promotion_exports_immutable_digest_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    assert "az acr repository show" in text
    assert "IMAGE_DIGEST" in text
    assert "export PROMOTION_ARTIFACT_REF=" in text
    assert "@$IMAGE_DIGEST" in text


# ===========================================================================
# deploy -> az containerapp update; consume digest on promotion
# ===========================================================================
def test_deploy_uses_containerapp_update_with_image() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "az containerapp update" in text
    assert "--image" in text


def test_deploy_without_promotion_uses_registry_image_refs() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "$ACR_LOGIN_SERVER" in text
    assert "$IMAGE_TAG" in text


def test_deploy_with_promotion_consumes_prebuilt_digest_no_rebuild() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "$PROMOTION_ARTIFACT_REF" in text
    assert "docker build" not in text
    assert "docker push" not in text
    assert "az containerapp update" in text


# ===========================================================================
# verify -> poll latest revision provisioningState to a terminal state
# ===========================================================================
def test_verify_polls_revision_provisioning_state() -> None:
    text = _joined(_plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod")))
    assert "az containerapp revision show" in text
    assert "provisioningState" in text
    assert "Provisioned" in text


# ===========================================================================
# lint / test are reasonable and generic
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
        "acr_login_server": "demoacr.azurecr.io",
        "acr_name": "demoacr",
        "image_name": "demo",
        "image_tag": "abc123",
        "container_app": "demo-app",
        "resource_group": "demo-rg",
    }
    build = _joined(_plugin().build(_ctx(StageVerb.BUILD, settings=settings)))
    assert "demoacr.azurecr.io/demo:abc123" in build
    deploy = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "demo-app" in deploy
    assert "demo-rg" in deploy


# ===========================================================================
# supply_iac -> Terraform mentioning Container App + env + ACR + identity
# ===========================================================================
def test_supply_iac_returns_files_under_platform_dir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    assert paths and all(p.startswith("iac/azure-container-apps/") for p in paths)


def test_iac_mentions_container_app_env_acr_and_identity() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "azurerm_container_app" in blob
    assert "azurerm_container_app_environment" in blob
    assert "azurerm_container_registry" in blob
    assert "azurerm_federated_identity_credential" in blob
    assert "azurerm_user_assigned_identity" in blob


def test_iac_references_region_from_ir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "uksouth" in blob


def test_iac_summary_describes_what_it_provisions() -> None:
    summary = _plugin().supply_iac(_pipeline()).summary.lower()
    assert "container app" in summary
    assert "acr" in summary
    assert "oidc" in summary or "federated" in summary


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
    assert not re.search(
        r"(?i)\b(password|client_secret|account_key)\s*[:=]\s*\S+", blob
    )
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "$ACR_NAME" in pkg or "$ACR_LOGIN_SERVER" in pkg
    deploy = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "$CONTAINER_APP" in deploy


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
