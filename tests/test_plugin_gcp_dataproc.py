"""Unit tests for the GCP Dataproc target-platform plugin (task 23.6, R4.2/R7.1, §11.9).

Loads the real ``plugins/target/gcp/dataproc`` package via the loader (no mocks, no
stand-in plugin), builds a :class:`VerbContext` from a representative gcp-dataproc
:class:`Pipeline`, calls each verb, and asserts the design §11.9 gcp-dataproc
mapping:

    * build   -> assemble the PySpark job script(s) + extra python libs/wheel (NO
                 image, pip-from-requirements so no live apt mirror) (R7.1);
    * package -> upload scripts/libs to GCS (immutable object GENERATION as the ref);
    * deploy  -> ``gcloud dataproc batches submit pyspark`` (submit a batch);
    * verify  -> poll the Dataproc batch state to SUCCEEDED;
    * lint/test are reasonable/generic (no Dockerfile lint).

Plus the IaC contract: :meth:`supply_iac` returns Terraform mentioning Dataproc +
GCS + SA + WIF and the region/project; promotion consumes a pre-built reference; and
the cross-cutting guarantees — secrets by reference only (R6), deterministic output
(R5.3), the data-job path declares no container_images capability.
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

# plugins/ live at <repo>/plugins; this test file is <repo>/tests/test_plugin_gcp_dataproc.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"

# The settings key the core threads for a promotion pipeline (design §11.6).
_PROMO_KEY = "promotion_artifact_ref"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_target_platform_plugin("gcp-dataproc", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    """A representative gcp-dataproc IR (GitHub + GCP Dataproc, OIDC)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "gcp-dataproc",
            "project": {"name": "demo-etl"},
            "cloud": {"provider": "gcp", "region": "europe-west2", "auth": "oidc"},
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
    assert plugin.name == "gcp-dataproc"


def test_all_six_verbs_dispatch_via_run_verb() -> None:
    plugin = _plugin()
    for verb in StageVerb:
        result = plugin.run_verb(_ctx(verb))
        assert result.commands, f"verb {verb} produced no commands"


def test_capabilities_declare_promotion_oidc_but_not_container_images() -> None:
    caps = _plugin().capabilities
    assert Capability.ARTIFACT_PROMOTION in caps
    assert Capability.OIDC in caps
    assert Capability.ENVIRONMENTS in caps
    assert Capability.SECRETS_BY_REFERENCE in caps
    # data-job path: no container image is built, so the capability is NOT declared.
    assert Capability.CONTAINER_IMAGES not in caps


# ===========================================================================
# build -> assemble scripts + libs, NO image, resilient (pip from requirements)
# ===========================================================================
def test_build_assembles_scripts_and_not_an_image() -> None:
    text = _joined(_plugin().build(_ctx(StageVerb.BUILD)))
    assert "scripts" in text.lower()
    assert "dataproc-extra-python-libs.zip" in text
    # NO docker / image anywhere on the data-job path.
    assert "docker build" not in text
    assert "docker push" not in text


def test_build_installs_extra_libs_from_requirements_no_apt_mirror() -> None:
    text = _joined(_plugin().build(_ctx(StageVerb.BUILD)))
    assert "pip install" in text
    assert "requirements.txt" in text
    low = text.lower()
    assert "apt-get update" not in low
    assert "apt-get install" not in low
    assert "apt install" not in low


def test_build_emits_committed_dataproc_batch_spec() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    paths = [f.path for f in result.files]
    assert "dataproc/dataproc-batch.json" in paths
    spec = next(f for f in result.files if f.path == "dataproc/dataproc-batch.json").content
    # a PySpark batch spec — no image anywhere
    assert "pysparkBatch" in spec
    assert "mainPythonFileUri" in spec
    assert "docker" not in spec.lower()


def test_build_libs_archive_is_reproducible_flag() -> None:
    # -X strips extra attributes so the archive bytes do not depend on host uid/gid.
    text = _joined(_plugin().build(_ctx(StageVerb.BUILD)))
    assert "zip -r -X" in text


# ===========================================================================
# package -> upload to GCS, capture immutable object generation (promotion)
# ===========================================================================
def test_package_uploads_scripts_to_gcs() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "gcloud storage cp" in text
    # no image push on the data-job path
    assert "docker push" not in text


def test_package_without_promotion_does_not_resolve_a_generation() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    # no promotion ref threaded -> no object-generation resolution / export
    assert "objects describe" not in text
    assert "#$OBJECT_GENERATION" not in text


def test_package_with_promotion_exports_immutable_generation_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    # resolves the immutable GCS object generation and exports the shared reference
    assert "objects describe" in text
    assert "generation" in text
    assert "export PROMOTION_ARTIFACT_REF=" in text
    assert "#$OBJECT_GENERATION" in text


# ===========================================================================
# deploy -> submit dataproc batch; reference pre-built ref on promotion
# ===========================================================================
def test_deploy_submits_a_dataproc_batch() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "gcloud dataproc batches submit pyspark" in text


def test_deploy_with_promotion_references_prebuilt_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    # consumes/announces the shared pre-built reference; does not re-upload a script
    assert "$PROMOTION_ARTIFACT_REF" in text
    assert "gcloud storage cp" not in text


# ===========================================================================
# verify -> poll batch state to SUCCEEDED
# ===========================================================================
def test_verify_polls_batch_state_to_terminal() -> None:
    text = _joined(_plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod")))
    assert "gcloud dataproc batches describe" in text
    assert "SUCCEEDED" in text
    assert "FAILED" in text


# ===========================================================================
# lint / test are reasonable and generic (no Dockerfile lint)
# ===========================================================================
def test_lint_targets_job_scripts_not_a_dockerfile() -> None:
    text = _joined(_plugin().lint(_ctx(StageVerb.LINT))).lower()
    assert "scripts" in text or "python" in text
    assert "dockerfile" not in text


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
        "batch_id": "demo-batch",
        "staging_bucket": "demo-bucket",
        "main_script_key": "dataproc/scripts/demo.py",
        "extra_libs_key": "dataproc/libs/demo.zip",
        "gcp_project": "demo-project",
    }
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    assert "demo-bucket" in pkg
    assert "dataproc/libs/demo.zip" in pkg
    deploy = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "demo-batch" in deploy


# ===========================================================================
# supply_iac -> Terraform mentioning Dataproc / GCS / SA / WIF and region/project
# ===========================================================================
def test_supply_iac_returns_terraform_files_under_platform_dir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    assert paths and all(p.startswith("iac/gcp-dataproc/") for p in paths)


def test_iac_mentions_dataproc_gcs_sa_and_wif() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "google_dataproc_batch" in blob
    assert "google_storage_bucket" in blob
    assert "google_service_account" in blob
    assert "google_iam_workload_identity_pool" in blob


def test_iac_references_region_and_project_from_ir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "europe-west2" in blob
    assert "var.project_id" in blob


def test_iac_summary_describes_what_it_provisions() -> None:
    summary = _plugin().supply_iac(_pipeline()).summary.lower()
    assert "dataproc" in summary
    assert "oidc" in summary or "workload identity" in summary


# ===========================================================================
# Secrets by reference only (R6) — no literal project specifics / secrets
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
    # no service account JSON key material committed
    assert "-----BEGIN PRIVATE KEY-----" not in blob
    assert '"private_key"' not in blob
    # no obvious literal secret/password assignments
    assert not re.search(
        r"(?i)\b(password|client_secret|private_key)\s*[:=]\s*\S+", blob
    )
    # project specifics are referenced by shell var when not set via settings
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "$DATAPROC_STAGING_BUCKET" in pkg
    assert "$GCP_PROJECT" in pkg


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
