"""Unit tests for the GCP GKE target-platform plugin (task 23.5, R4.2/R7.1, §11.9).

Loads the real ``plugins/target/gcp/gke`` package via the loader (no mocks, no
stand-in plugin), builds a :class:`VerbContext` from a representative gcp-gke
:class:`Pipeline`, calls each verb, and asserts the design §11.9 gcp-gke mapping:

    * build   -> ``docker build`` (+ a resilient, pinned, apt-mirror-free Dockerfile,
                 R7.1) — GKE is container-based;
    * package -> push the image to Google Artifact Registry (immutable DIGEST as the
                 promotable ref);
    * deploy  -> ``gcloud container clusters get-credentials`` + ``kubectl`` / ``helm``;
    * verify  -> ``kubectl rollout status``;
    * lint/test are reasonable/generic (Dockerfile lint, like aws-ecs).

Plus the IaC contract: :meth:`supply_iac` returns Terraform + manifests mentioning
GKE / Artifact Registry / Workload Identity / WIF and the region + project;
promotion consumes a pre-built digest reference; and the cross-cutting guarantees —
secrets by reference only (R6), deterministic output (R5.3), the container
capability set of §11.9.

GKE is the GCP marriage of gcp-cloud-run (Artifact Registry + build/push a container
image) and the generic kubernetes/aws-eks plugins (kubectl/helm deploy + rollout).
It is GKE-specific — ``gcloud container clusters get-credentials`` (NOT ``aws eks
update-kubeconfig``), Artifact Registry (NOT ECR), GKE Workload Identity (NOT IRSA) —
differentiated like gcp-cloud-run differs from aws-eks.
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

# plugins/ live at <repo>/plugins; this test file is <repo>/tests/test_plugin_gcp_gke.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"

# The settings key the core threads for a promotion pipeline (design §11.6).
_PROMO_KEY = "promotion_artifact_ref"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_target_platform_plugin("gcp-gke", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    """A representative gcp-gke IR (GitHub + GCP GKE, OIDC)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "gcp-gke",
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
# Prefix / layout resolution + discovery (gcp/gke is now the NATIVE plugin)
# ===========================================================================
def test_prefix_split_resolves_gcp_gke_to_nested_dir() -> None:
    # The gcp- prefix rule maps gcp-gke -> provider "gcp" / service "gke".
    assert resolve_target_dir("gcp-gke") == ("gcp", "gke")


def test_discovery_round_trips_gcp_gke_ir_name() -> None:
    # gcp/gke now reports its IR name as the native "gcp-gke" (NOT "kubernetes").
    assert ir_name_for_dir("gcp", "gke") == "gcp-gke"


def test_discovery_includes_gcp_gke() -> None:
    available = AvailablePlugins.discover(REPO_ROOT)
    assert "gcp-gke" in available.target


def test_discovery_still_includes_legacy_kubernetes() -> None:
    # The legacy generic kubernetes plugin relocated to k8s/generic is still found.
    available = AvailablePlugins.discover(REPO_ROOT)
    assert "kubernetes" in available.target


# ===========================================================================
# Loader / identity
# ===========================================================================
def test_plugin_loads_by_name() -> None:
    plugin = _plugin()
    assert plugin.name == "gcp-gke"


def test_gcp_gke_is_not_the_generic_kubernetes_plugin() -> None:
    # gcp-gke must be a DISTINCT, GKE-specific plugin — not the relocated generic one.
    gke = _plugin()
    k8s = load_target_platform_plugin("kubernetes", plugins_root=PLUGINS_ROOT)
    assert type(gke) is not type(k8s)
    assert gke.name == "gcp-gke"
    assert k8s.name == "kubernetes"


def test_all_six_verbs_dispatch_via_run_verb() -> None:
    plugin = _plugin()
    for verb in StageVerb:
        result = plugin.run_verb(_ctx(verb))
        assert result.commands, f"verb {verb} produced no commands"


def test_capabilities_declare_container_promotion_oidc_and_more() -> None:
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
    assert ":latest" not in dockerfile.content
    assert "pip install" in low
    assert "apt-get update" not in low
    assert "apt-get install" not in low


def test_build_dockerfile_is_multistage() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    dockerfile = next(f for f in result.files if f.path == "Dockerfile")
    stages = re.findall(r"(?mi)^FROM\s+\S+\s+AS\s+\w+", dockerfile.content)
    assert len(stages) >= 2


# ===========================================================================
# package -> push to Artifact Registry, capture immutable digest (promotion)
# ===========================================================================
def test_package_pushes_to_artifact_registry() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "gcloud auth configure-docker" in text
    assert "docker.pkg.dev" in text
    assert "docker push" in text
    # GKE uses Artifact Registry, NOT ECR.
    assert "aws ecr" not in text.lower()


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
# deploy -> gcloud get-credentials + kubectl/helm; consume digest on promotion
# ===========================================================================
def test_deploy_fetches_gke_credentials_and_uses_kubectl_or_helm() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    # GKE-native credentials — NOT aws eks update-kubeconfig.
    assert "gcloud container clusters get-credentials" in text
    assert "aws eks update-kubeconfig" not in text
    assert "kubectl" in text
    assert "helm upgrade --install" in text


def test_deploy_without_promotion_uses_artifact_registry_image_ref() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "-docker.pkg.dev/" in text
    assert "$GKE_CLUSTER" in text


def test_deploy_with_promotion_consumes_prebuilt_digest_no_rebuild() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    # consumes the shared pre-built reference; does not rebuild/re-push the image
    assert "$PROMOTION_ARTIFACT_REF" in text
    assert "docker build" not in text
    assert "docker push" not in text
    assert "gcloud container clusters get-credentials" in text
    assert "kubectl" in text


# ===========================================================================
# verify -> kubectl rollout status
# ===========================================================================
def test_verify_checks_rollout_status() -> None:
    text = _joined(_plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod")))
    assert "kubectl" in text
    assert "rollout status" in text


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
        "gke_cluster": "demo-cluster",
        "gke_location": "europe-west1",
        "namespace": "demo-ns",
        "deployment_name": "demo-deploy",
        "kube_context": "demo-ctx",
    }
    build = _joined(_plugin().build(_ctx(StageVerb.BUILD, settings=settings)))
    assert "europe-west1-docker.pkg.dev/my-proj/demo-repo/demo-img:abc123" in build
    deploy = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "demo-cluster" in deploy
    assert "demo-ns" in deploy
    assert "demo-deploy" in deploy
    assert "my-proj" in deploy


# ===========================================================================
# supply_iac -> Terraform + manifests mentioning GKE / AR / Workload Identity / WIF
# ===========================================================================
def test_supply_iac_returns_files_under_platform_dir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    assert any(p.endswith("outputs.tf") for p in paths)
    assert any(p.endswith("deployment.yaml") for p in paths)
    assert any(p.endswith("serviceaccount.yaml") for p in paths)
    assert paths and all(p.startswith("iac/gcp-gke/") for p in paths)


def test_iac_references_existing_gke_cluster_and_provisions_artifact_registry() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    # GKE cluster referenced (data source, not created), Artifact Registry provisioned.
    assert "google_container_cluster" in blob
    assert "data \"google_container_cluster\"" in blob
    assert "google_artifact_registry_repository" in blob


def test_iac_wires_gke_workload_identity_and_wif() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    # GKE Workload Identity: the KSA->GSA binding + the GSA annotation in the manifest.
    assert "roles/iam.workloadIdentityUser" in blob
    assert "svc.id.goog" in blob
    assert "iam.gke.io/gcp-service-account" in blob  # GKE WI annotation in the manifest
    # Workload Identity Federation pool + provider (keyless CI).
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
    assert "gke" in summary
    assert "artifact registry" in summary
    assert "workload identity" in summary
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
    # project/cluster specifics are referenced by shell var when not set via settings
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "$GCP_PROJECT" in pkg
    deploy = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "$GKE_CLUSTER" in deploy
    assert "$NAMESPACE" in deploy


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
