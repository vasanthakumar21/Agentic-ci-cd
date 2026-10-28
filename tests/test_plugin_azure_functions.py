"""Unit tests for the Azure Functions target-platform plugin (task 12.1, R4.2/§8b).

Loads the real ``plugins/target/azure-functions`` package via the loader (no mocks,
no stand-in plugin), builds a :class:`VerbContext` from a representative
azure-functions :class:`Pipeline` (cloud.provider azure), calls each verb, and
asserts the design §8b azure-functions mapping:

    * build   -> build a function package (deps into a build dir), NO docker image;
    * package -> zip + stage the function app into a deployable artifact;
    * deploy  -> ``func azure functionapp publish`` / ``az functionapp ... config-zip``
                 referencing the app by reference;
    * verify  -> a function smoke test (``az functionapp show`` + curl);
    * lint/test are reasonable/generic.

Plus the IaC contract: :meth:`supply_iac` returns Terraform mentioning a Function App
resource (``azurerm_linux_function_app`` / ``azurerm_function_app``) + a storage
account + a service plan and references the region; and the cross-cutting guarantees
— secrets by reference only (R6) and deterministic output (R5.3). The HCL is
validated structurally with ``cicdgen.validate.validate_hcl_structure`` (per file;
``validate_terraform_module`` requires ``provider "aws"`` so it is NOT used here).
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
from cicdgen.validate import TerraformHclValidationError, validate_hcl_structure

# plugins/ live at <repo>/plugins; this file is <repo>/tests/test_plugin_azure_functions.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"

#: A Function App resource is either of these azurerm resource types (§8b).
FUNCTION_APP_RESOURCES = ("azurerm_linux_function_app", "azurerm_function_app")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_target_platform_plugin("azure-functions", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    """A representative azure-functions IR (GitHub + Azure, OIDC)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "azure-functions",
            "project": {"name": "demo-func"},
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


def _iac_files() -> dict[str, str]:
    """Freshly generate the azure-functions IaC keyed by basename."""
    iac = _plugin().supply_iac(_pipeline())
    return {Path(f.path).name: f.content for f in iac.files}


# ===========================================================================
# Loader / identity
# ===========================================================================
def test_plugin_loads_by_name() -> None:
    assert _plugin().name == "azure-functions"


def test_all_six_verbs_dispatch_via_run_verb() -> None:
    plugin = _plugin()
    for verb in StageVerb:
        result = plugin.run_verb(_ctx(verb))
        assert result.commands, f"verb {verb} produced no commands"


# ===========================================================================
# build -> function package, NO docker image
# ===========================================================================
def test_build_produces_a_function_package() -> None:
    text = _joined(_plugin().build(_ctx(StageVerb.BUILD)))
    # installs deps into a build dir / produces the function app artifact
    assert "pip install" in text
    assert "mkdir" in text


def test_build_does_not_build_or_push_a_docker_image() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    text = _joined(result).lower()
    assert "docker build" not in text
    assert "docker push" not in text
    # no Dockerfile support file either
    paths = [f.path.lower() for f in result.files]
    assert not any("dockerfile" in p for p in paths)


# ===========================================================================
# package -> zip + stage the function app
# ===========================================================================
def test_package_zips_and_stages_the_app() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "zip" in text
    assert "functionapp.zip" in text
    # not an image push
    assert "docker push" not in text.lower()


# ===========================================================================
# deploy -> func publish / az functionapp config-zip, app by reference
# ===========================================================================
def test_deploy_publishes_the_function_app() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "func azure functionapp publish" in text or "az functionapp deployment" in text
    # app referenced by $REF when no settings supplied
    assert "$AZURE_FUNCTION_APP" in text


# ===========================================================================
# verify -> function smoke test
# ===========================================================================
def test_verify_smoke_tests_the_function() -> None:
    text = _joined(_plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod")))
    assert "az functionapp show" in text or "curl" in text


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
    result = _plugin().test(ctx)
    assert result.commands


# ===========================================================================
# settings override non-secret knobs (value instead of $REF)
# ===========================================================================
def test_settings_inline_non_secret_knobs() -> None:
    settings = {
        "function_app": "demo-func-app",
        "resource_group": "demo-rg",
    }
    deploy = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "demo-func-app" in deploy
    verify = _joined(
        _plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod", settings=settings))
    )
    assert "demo-func-app" in verify
    assert "demo-rg" in verify


def test_region_falls_back_to_ir_then_ref() -> None:
    # IR carries uksouth -> appears in IaC; verbs that need region use it too.
    blob = "\n".join(f.content for f in _plugin().supply_iac(_pipeline()).files)
    assert "uksouth" in blob


# ===========================================================================
# supply_iac -> Function App + storage account + service plan, region
# ===========================================================================
def test_supply_iac_returns_terraform_files() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    assert paths and all(p.startswith("iac/azure-functions/") for p in paths)


def test_iac_mentions_function_app_storage_and_service_plan() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert any(res in blob for res in FUNCTION_APP_RESOURCES)
    assert "azurerm_storage_account" in blob
    assert "azurerm_service_plan" in blob
    # a user-assigned identity / OIDC federated credential for keyless CI
    assert "azurerm_user_assigned_identity" in blob
    assert "azurerm_federated_identity_credential" in blob


def test_iac_references_region_from_ir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "uksouth" in blob  # the IR region surfaces as the location default


def test_iac_uses_azurerm_provider() -> None:
    main = _iac_files()["main.tf"]
    assert "hashicorp/azurerm" in main
    assert 'provider "azurerm"' in main
    assert "required_providers" in main


def test_iac_summary_describes_what_it_provisions() -> None:
    summary = _plugin().supply_iac(_pipeline()).summary.lower()
    assert "function app" in summary
    assert "storage" in summary
    assert "identity" in summary or "oidc" in summary


# ===========================================================================
# HCL is structurally valid (provider-agnostic per-file validator)
# ===========================================================================
def test_each_tf_file_passes_structural_validator() -> None:
    files = _iac_files()
    for name in ("main.tf", "variables.tf", "outputs.tf"):
        validate_hcl_structure(files[name], filename=name)  # raises on malformed HCL


def test_tfvars_example_is_balanced() -> None:
    content = _iac_files()["terraform.tfvars.example"]
    assert content.count("{") == content.count("}")
    assert content.count("[") == content.count("]")


def test_structural_validator_rejects_broken_azure_hcl() -> None:
    import pytest

    bad = 'resource "azurerm_linux_function_app" "x" {\n  name = "y"\n'  # no close brace
    with pytest.raises(TerraformHclValidationError):
        validate_hcl_structure(bad, filename="bad.tf")


# ===========================================================================
# Secrets by reference only (R6) — no literal account specifics / secrets
# ===========================================================================
def test_no_literal_secrets_or_account_specifics() -> None:
    blob = _all_generated_text(_plugin())
    # no obvious literal secret/password/connection-string assignments
    assert not re.search(
        r"(?i)\b(password|secret|connection_string|account_key)\s*[:=]\s*\"[^\"$]+\"",
        blob,
    )
    # no bare subscription-id GUID assigned anywhere
    assert not re.search(
        r"(?i)subscription_id\s*[:=]\s*\"[0-9a-f-]{36}\"", blob
    )
    # account specifics are referenced by shell var when not set via settings
    deploy = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "$AZURE_FUNCTION_APP" in deploy
    assert "$AZURE_RESOURCE_GROUP" in deploy


def test_iac_references_connection_by_resource_not_literal() -> None:
    """The storage connection is wired by resource reference, never a literal (R6)."""
    main = _iac_files()["main.tf"]
    # the Function App reads the storage key by resource reference
    assert "azurerm_storage_account.func.primary_access_key" in main
    # no literal connection string committed
    for line in main.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "description" in stripped:
            continue
        assert "DefaultEndpointsProtocol=" not in stripped
        assert "AccountKey=" not in stripped


# ===========================================================================
# NO container image anywhere (azure-functions is NOT docker-shaped)
# ===========================================================================
def test_no_verb_emits_docker_or_references_ecr_ecs() -> None:
    blob = _all_generated_text(_plugin())
    # drop the explicit "NO ECR"/"NO ECS" negations (prose documenting the proof)
    blob = re.sub(r"NO EC[RS]", "", blob)
    lowered = blob.lower()
    assert "docker build" not in lowered
    assert "docker push" not in lowered
    assert "docker login" not in lowered
    assert not re.search(r"\becr\b", lowered)
    assert not re.search(r"\becs\b", lowered)
    assert "aws_ecs" not in lowered
    assert "aws_ecr" not in lowered


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
    iac = _plugin().supply_iac(_pipeline())
    assert all(isinstance(f, GeneratedFile) for f in iac.files)
