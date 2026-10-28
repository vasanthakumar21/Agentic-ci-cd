"""Unit tests for the Kubernetes target-platform plugin (task 12.1, R4.2/R7.1, §8b).

Loads the real ``plugins/target/kubernetes`` package via the loader (no mocks, no
stand-in plugin), builds a :class:`VerbContext` from a representative kubernetes
:class:`Pipeline`, calls each verb, and asserts the design §8b kubernetes mapping:

    * build   -> ``docker build`` + emits a resilient Dockerfile (pinned base,
                 NO fragile apt-mirror step, multi-stage) (R7.1);
    * package -> ``docker push`` to a (generic) registry reference;
    * deploy  -> ``kubectl apply`` / ``helm upgrade``;
    * verify  -> ``kubectl rollout status``;
    * lint/test are reasonable/generic.

Plus the IaC contract: :meth:`supply_iac` emits k8s Deployment/Service manifests
(``kind: Deployment``) AND a kubernetes-provider Terraform module
(``kubernetes_deployment``) referencing image/namespace by variable; and the
cross-cutting guarantees — secrets by reference only (R6), deterministic output
(R5.3), and structurally valid HCL (validated provider-agnostically).
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
from cicdgen.validate import validate_hcl_structure

# plugins/ live at <repo>/plugins; this file is <repo>/tests/test_plugin_kubernetes.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_target_platform_plugin("kubernetes", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    """A representative kubernetes IR (GitHub + a Kubernetes cluster)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "kubernetes",
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
    assert plugin.name == "kubernetes"


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
# package -> docker push to a (generic) registry reference
# ===========================================================================
def test_package_pushes_image_to_registry() -> None:
    result = _plugin().package(_ctx(StageVerb.PACKAGE))
    text = _joined(result)
    assert "docker push" in text
    # generic registry reference by shell var — NOT ECR-specific
    assert "$IMAGE_REPO" in text
    assert "aws ecr" not in text.lower()


# ===========================================================================
# deploy -> kubectl apply / helm upgrade
# ===========================================================================
def test_deploy_applies_with_kubectl_or_helm() -> None:
    result = _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod"))
    text = _joined(result)
    assert "kubectl" in text and "apply" in text
    assert "helm upgrade" in text


# ===========================================================================
# verify -> kubectl rollout status
# ===========================================================================
def test_verify_checks_rollout_status() -> None:
    result = _plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod"))
    text = _joined(result)
    assert "kubectl" in text
    assert "rollout status" in text


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
        "image_repo": "registry.example.com/demo-svc",
        "image_tag": "abc123",
        "namespace": "demo-ns",
        "deployment_name": "demo-deploy",
        "kube_context": "demo-ctx",
    }
    build = _plugin().build(_ctx(StageVerb.BUILD, settings=settings))
    assert "registry.example.com/demo-svc:abc123" in _joined(build)
    deploy = _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    text = _joined(deploy)
    assert "demo-ns" in text
    assert "demo-deploy" in text
    verify = _plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod", settings=settings))
    assert "demo-deploy" in _joined(verify)


# ===========================================================================
# supply_iac -> k8s manifests (kind: Deployment) + kubernetes-provider Terraform
# ===========================================================================
def test_supply_iac_returns_manifests_and_terraform() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("deployment.yaml") for p in paths)
    assert any(p.endswith("service.yaml") for p in paths)
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    # all IaC lives under the per-platform iac dir
    assert paths and all(p.startswith("iac/kubernetes/") for p in paths)


def test_iac_manifests_declare_deployment_and_service() -> None:
    iac = _plugin().supply_iac(_pipeline())
    by_path = {f.path: f.content for f in iac.files}
    deployment = by_path["iac/kubernetes/deployment.yaml"]
    service = by_path["iac/kubernetes/service.yaml"]
    assert "kind: Deployment" in deployment
    assert "kind: Service" in service
    # image + namespace are placeholders, never literals
    assert "${IMAGE_REPO}" in deployment
    assert "${NAMESPACE}" in deployment


def test_iac_terraform_uses_kubernetes_provider_resources() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "kubernetes_deployment" in blob
    assert "kubernetes_service" in blob
    assert 'provider "kubernetes"' in blob
    # image + namespace referenced by variable, never literal
    assert "var.image_repo" in blob
    assert "var.namespace" in blob


def test_iac_terraform_is_structurally_valid_hcl() -> None:
    # validate each .tf file provider-agnostically
    iac = _plugin().supply_iac(_pipeline())
    tf_files = [f for f in iac.files if f.path.endswith(".tf")]
    assert tf_files
    for f in tf_files:
        validate_hcl_structure(f.content, filename=f.path)


def test_iac_references_region_from_ir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    # the IR region surfaces (as the region default / tfvars example)
    assert "eu-west-2" in blob


def test_iac_summary_describes_what_it_provisions() -> None:
    iac = _plugin().supply_iac(_pipeline())
    summary = iac.summary.lower()
    assert "deployment" in summary
    assert "service" in summary
    assert "kubernetes" in summary


# ===========================================================================
# Secrets by reference only (R6) — no literal environment specifics / secrets
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


def test_no_literal_secrets_without_settings() -> None:
    # Build everything WITHOUT inlining settings -> environment specifics must be refs.
    blob = _all_generated_text(_plugin())
    # no obvious literal secret/password/token assignments
    assert not re.search(r"(?i)\b(password|secret_key|bearer_token)\s*[:=]\s*\S+", blob)
    # no base64 kubeconfig / no literal kube token / cert material
    assert "BEGIN CERTIFICATE" not in blob
    # the image repo / namespace are referenced by shell var when not set via settings
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "$IMAGE_REPO" in pkg
    deploy = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
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
def test_generated_files_are_generatedfile_instances() -> None:
    build = _plugin().build(_ctx(StageVerb.BUILD))
    assert all(isinstance(f, GeneratedFile) for f in build.files)
    iac = _plugin().supply_iac(_pipeline())
    assert all(isinstance(f, GeneratedFile) for f in iac.files)
