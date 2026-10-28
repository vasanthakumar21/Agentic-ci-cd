"""Unit tests for the AWS EKS target-platform plugin (task 23.2, R4.2/R7.1, §11.9).

Loads the real ``plugins/target/aws/eks`` package via the loader (no mocks, no
stand-in plugin), builds a :class:`VerbContext` from a representative aws-eks
:class:`Pipeline`, calls each verb, and asserts the design §11.9 aws-eks mapping:

    * build   -> ``docker build`` (+ a resilient, pinned, apt-mirror-free Dockerfile,
                 R7.1) — EKS is container-based;
    * package -> push the image to ECR (immutable DIGEST as the promotable ref);
    * deploy  -> refresh kube creds + ``kubectl apply`` / ``helm upgrade``;
    * verify  -> ``kubectl rollout status``;
    * lint/test are reasonable/generic (Dockerfile lint, like aws-ecs).

Plus the IaC contract: :meth:`supply_iac` returns Terraform + manifests mentioning
EKS / ECR / IRSA / OIDC and the region; promotion consumes a pre-built digest
reference; and the cross-cutting guarantees — secrets by reference only (R6),
deterministic output (R5.3), the container capability set of §11.9.
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

# plugins/ live at <repo>/plugins; this test file is <repo>/tests/test_plugin_aws_eks.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"

# The settings key the core threads for a promotion pipeline (design §11.6).
_PROMO_KEY = "promotion_artifact_ref"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_target_platform_plugin("aws-eks", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    """A representative aws-eks IR (GitHub + AWS EKS, OIDC)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-eks",
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
    assert plugin.name == "aws-eks"


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
# package -> push to ECR, capture immutable digest (promotion)
# ===========================================================================
def test_package_pushes_to_ecr() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "aws ecr get-login-password" in text
    assert "docker push" in text


def test_package_without_promotion_does_not_resolve_a_digest() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "describe-images" not in text
    assert "imageDigest" not in text


def test_package_with_promotion_exports_immutable_digest_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    # resolves the immutable image digest and exports the shared reference
    assert "aws ecr describe-images" in text
    assert "imageDigest" in text
    assert "export PROMOTION_ARTIFACT_REF=" in text
    assert "@$IMAGE_DIGEST" in text


# ===========================================================================
# deploy -> kubectl/helm apply; consume pre-built digest on promotion
# ===========================================================================
def test_deploy_uses_kubectl_or_helm_and_refreshes_kubeconfig() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "aws eks update-kubeconfig" in text
    assert "kubectl" in text
    assert "helm upgrade --install" in text


def test_deploy_without_promotion_uses_ecr_repo_tag_refs() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "$ECR_REPO" in text
    assert "$IMAGE_TAG" in text


def test_deploy_with_promotion_consumes_prebuilt_digest_no_rebuild() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    # consumes the shared pre-built reference; does not rebuild/re-push the image
    assert "$PROMOTION_ARTIFACT_REF" in text
    assert "docker build" not in text
    assert "docker push" not in text
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
        "ecr_repo": "123.dkr.ecr.eu-west-2.amazonaws.com/demo",
        "image_tag": "abc123",
        "eks_cluster": "demo-cluster",
        "namespace": "demo-ns",
        "deployment_name": "demo-deploy",
        "kube_context": "demo-ctx",
    }
    build = _joined(_plugin().build(_ctx(StageVerb.BUILD, settings=settings)))
    assert "123.dkr.ecr.eu-west-2.amazonaws.com/demo:abc123" in build
    deploy = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "demo-cluster" in deploy
    assert "demo-ns" in deploy
    assert "demo-deploy" in deploy


# ===========================================================================
# supply_iac -> Terraform + manifests mentioning EKS / ECR / IRSA / OIDC + region
# ===========================================================================
def test_supply_iac_returns_files_under_platform_dir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    assert any(p.endswith("deployment.yaml") for p in paths)
    assert any(p.endswith("serviceaccount.yaml") for p in paths)
    assert paths and all(p.startswith("iac/aws-eks/") for p in paths)


def test_iac_mentions_eks_ecr_irsa_and_oidc() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    # EKS referenced (not created), ECR provisioned, IRSA + CI OIDC wired.
    assert "aws_eks_cluster" in blob
    assert "aws_ecr_repository" in blob
    assert "irsa" in blob.lower()
    assert "eks.amazonaws.com/role-arn" in blob  # IRSA annotation in the manifest
    assert "aws_iam_openid_connect_provider" in blob


def test_iac_references_region_from_ir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "eu-west-2" in blob


def test_iac_summary_describes_what_it_provisions() -> None:
    summary = _plugin().supply_iac(_pipeline()).summary.lower()
    assert "eks" in summary
    assert "irsa" in summary or "oidc" in summary
    assert "ecr" in summary


# ===========================================================================
# Secrets by reference only (R6) — no literal account specifics / secrets
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
    # no account-specific IAM ARNs committed (managed-policy aws:policy arns are ok)
    assert not re.search(r"arn:aws:iam::\d{12}:(role|user)/", blob)
    # no AWS access key id shapes
    assert not re.search(r"AKIA[0-9A-Z]{16}", blob)
    # no obvious literal secret/password assignments
    assert not re.search(
        r"(?i)\b(password|secret_key|aws_secret_access_key)\s*[:=]\s*\S+", blob
    )
    # account/environment specifics are referenced by shell var when not set via settings
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "$ECR_REPO" in pkg
    deploy = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "$EKS_CLUSTER" in deploy


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
