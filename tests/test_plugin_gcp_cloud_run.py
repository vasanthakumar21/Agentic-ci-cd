"""Unit tests for the GCP Cloud Run target-platform plugin (task 23.4, R4.2/R7.1, §11.9).

Loads the real ``plugins/target/gcp/cloud-run`` package via the loader (no mocks, no
stand-in plugin), builds a :class:`VerbContext` from a representative gcp-cloud-run
:class:`Pipeline`, calls each verb, and asserts the design §11.9 gcp-cloud-run
mapping:

    * build   -> ``docker build`` (+ a resilient, pinned, apt-mirror-free Dockerfile,
                 R7.1) — Cloud Run is container-based;
    * package -> push the image to Google Artifact Registry (immutable DIGEST as the
                 promotable ref);
    * deploy  -> ``gcloud run deploy`` consuming the pre-built image under promotion;
    * verify  -> the new revision is serving/healthy (``gcloud run services describe``
                 + Ready condition);
    * lint/test are reasonable/generic (Dockerfile lint, like aws-ecs).

Plus the IaC contract: :meth:`supply_iac` returns Terraform mentioning Cloud Run /
Artifact Registry / WIF / service account and the region + project; and the
cross-cutting guarantees — secrets by reference only (R6), deterministic output
(R5.3), the container capability set of §11.9.

Also checks the ``gcp-`` prefix resolution handles the multi-hyphen service name
``cloud-run`` correctly (``gcp-cloud-run`` -> provider ``gcp`` / service ``cloud-run``,
splitting on the FIRST ``-``), both for resolution and for discovery round-trip.
"""

from __future__ import annotations

import re
from pathlib import Path

from cicdgen.ir import AvailablePlugins, Pipeline
from cicdgen.plugins import (
    GeneratedFile,
    StageVerb,
    VerbContext,
    load_target_platform_plugin,
)
from cicdgen.plugins.base import Capability
from cicdgen.plugins.target_layout import ir_name_for_dir, resolve_target_dir

# plugins/ live at <repo>/plugins; this test file is
# <repo>/tests/test_plugin_gcp_cloud_run.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"

# The settings key the core threads for a promotion pipeline (design §11.6).
_PROMO_KEY = "promotion_artifact_ref"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_target_platform_plugin("gcp-cloud-run", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    """A representative gcp-cloud-run IR (GitHub + GCP Cloud Run, OIDC)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "gcp-cloud-run",
            "project": {"name": "demo-svc"},
            "cloud": {"provider": "gcp", "region": "us-central1", "auth": "oidc"},
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
# Prefix / layout resolution for the multi-hyphen service name "cloud-run"
# ===========================================================================
def test_prefix_split_handles_multi_hyphen_service_name() -> None:
    # Splits on the FIRST '-': provider "gcp", service "cloud-run" (NOT "cloud").
    assert resolve_target_dir("gcp-cloud-run") == ("gcp", "cloud-run")


def test_discovery_round_trips_gcp_cloud_run_ir_name() -> None:
    # The nested dir reports its IR name back as "gcp-cloud-run".
    assert ir_name_for_dir("gcp", "cloud-run") == "gcp-cloud-run"


def test_discovery_includes_gcp_cloud_run() -> None:
    available = AvailablePlugins.discover(REPO_ROOT)
    assert "gcp-cloud-run" in available.target


# ===========================================================================
# Loader / identity
# ===========================================================================
def test_plugin_loads_by_name() -> None:
    plugin = _plugin()
    assert plugin.name == "gcp-cloud-run"


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
    paths = [f.path for f in result.files]
    assert "Dockerfile" in paths


def test_build_tags_image_with_artifact_registry_reference() -> None:
    text = _joined(_plugin().build(_ctx(StageVerb.BUILD)))
    # region-docker.pkg.dev/<project>/<repo>/<image>:<tag>, all by-reference
    assert "-docker.pkg.dev/" in text


def test_build_dockerfile_is_resilient_no_apt_mirror() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    dockerfile = next(f for f in result.files if f.path == "Dockerfile")
    low = dockerfile.content.lower()
    # pinned base, pip from requirements, and NO live apt mirror step (R7.1)
    assert "python:3.12-slim" in dockerfile.content
    assert "pip install" in low
    assert "apt-get update" not in low
    assert "apt-get install" not in low


# ===========================================================================
# package -> push to Artifact Registry, capture immutable digest (promotion)
# ===========================================================================
def test_package_pushes_to_artifact_registry() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "gcloud auth configure-docker" in text
    assert "docker.pkg.dev" in text
    assert "docker push" in text


def test_package_without_promotion_does_not_resolve_a_digest() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "images describe" not in text
    assert "IMAGE_DIGEST" not in text


def test_package_with_promotion_exports_immutable_digest_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    # resolves the immutable image digest and exports the shared reference
    assert "gcloud artifacts docker images describe" in text
    assert "IMAGE_DIGEST" in text
    assert "export PROMOTION_ARTIFACT_REF=" in text
    assert "@$IMAGE_DIGEST" in text


# ===========================================================================
# deploy -> gcloud run deploy; consume pre-built digest on promotion
# ===========================================================================
def test_deploy_uses_gcloud_run_deploy() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "gcloud run deploy" in text
    assert "--image" in text


def test_deploy_without_promotion_uses_artifact_registry_image_ref() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "-docker.pkg.dev/" in text
    assert "$CLOUD_RUN_SERVICE" in text


def test_deploy_with_promotion_consumes_prebuilt_digest_no_rebuild() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    # consumes the shared pre-built reference; does not rebuild/re-push the image
    assert "$PROMOTION_ARTIFACT_REF" in text
    assert "docker build" not in text
    assert "docker push" not in text
    assert "gcloud run deploy" in text


# ===========================================================================
# verify -> revision health (gcloud run services describe / Ready condition)
# ===========================================================================
def test_verify_checks_revision_health() -> None:
    text = _joined(_plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod")))
    assert "gcloud run services describe" in text
    # polls the Ready condition of the serving revision
    assert "Ready" in text


# ===========================================================================
# lint / test are reasonable and generic (Dockerfile lint, like aws-ecs)
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
    result = _plugin().test(ctx)
    assert result.commands


# ===========================================================================
# settings override non-secret knobs (value instead of $REF)
# ===========================================================================
def test_settings_inline_non_secret_knobs() -> None:
    settings = {
        "gcp_project": "my-proj",
        "gcp_region": "europe-west1",
        "ar_repo": "demo-repo",
        "image_name": "demo-img",
        "image_tag": "abc123",
        "service_name": "demo-service",
    }
    build = _joined(_plugin().build(_ctx(StageVerb.BUILD, settings=settings)))
    assert "europe-west1-docker.pkg.dev/my-proj/demo-repo/demo-img:abc123" in build
    deploy = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "demo-service" in deploy
    assert "my-proj" in deploy


# ===========================================================================
# supply_iac -> Terraform mentioning Cloud Run / Artifact Registry / WIF / SA
# ===========================================================================
def test_supply_iac_returns_files_under_platform_dir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    assert any(p.endswith("outputs.tf") for p in paths)
    assert paths and all(p.startswith("iac/gcp-cloud-run/") for p in paths)


def test_iac_mentions_cloud_run_artifact_registry_wif_and_service_account() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "google_cloud_run_v2_service" in blob
    assert "google_artifact_registry_repository" in blob
    # Workload Identity Federation pool + provider (the GCP keyless-OIDC story).
    assert "google_iam_workload_identity_pool" in blob
    assert "google_iam_workload_identity_pool_provider" in blob
    # deploy service account.
    assert "google_service_account" in blob


def test_iac_references_region_and_project_by_reference() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    # region drives the provider default; project id is a variable (never a literal).
    assert "us-central1" in blob
    assert "var.project_id" in blob


def test_iac_summary_describes_what_it_provisions() -> None:
    summary = _plugin().supply_iac(_pipeline()).summary.lower()
    assert "cloud run" in summary
    assert "artifact registry" in summary
    assert "workload identity" in summary or "wif" in summary
    assert "service account" in summary


# ===========================================================================
# Secrets by reference only (R6) — no literal project specifics / secrets / keys
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


def test_no_literal_secrets_or_project_specifics_without_settings() -> None:
    blob = _all_generated_text(_plugin())
    # no service account JSON private-key markers committed
    assert "private_key" not in blob
    assert "-----BEGIN PRIVATE KEY-----" not in blob
    # no obvious literal secret/password assignments
    assert not re.search(
        r"(?i)\b(password|secret_key|client_secret)\s*[:=]\s*\S+", blob
    )
    # project/service specifics are referenced by shell var when not set via settings
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "$GCP_PROJECT" in pkg
    deploy = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "$CLOUD_RUN_SERVICE" in deploy


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
def test_generated_iac_files_are_generatedfile_instances() -> None:
    iac = _plugin().supply_iac(_pipeline())
    assert all(isinstance(f, GeneratedFile) for f in iac.files)
