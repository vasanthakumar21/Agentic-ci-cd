"""Unit tests for the AWS Glue target-platform plugin (task 23.3, R4.2/R7.1, §11.9).

Loads the real ``plugins/target/aws/glue`` package via the loader (no mocks, no
stand-in plugin), builds a :class:`VerbContext` from a representative aws-glue
:class:`Pipeline`, calls each verb, and asserts the design §11.9 aws-glue mapping:

    * build   -> assemble the Glue job script(s) + extra python libs/wheel (NO
                 image, pip-from-requirements so no live apt mirror) (R7.1);
    * package -> upload scripts/libs to S3 (immutable object VERSION as the ref);
    * deploy  -> ``aws glue create-job`` / ``update-job`` (create/update the job);
    * verify  -> ``aws glue start-job-run`` + poll run state to SUCCEEDED;
    * lint/test are reasonable/generic (no Dockerfile lint).

Plus the IaC contract: :meth:`supply_iac` returns Terraform mentioning the Glue job
+ IAM + S3 + OIDC and the region; promotion consumes a pre-built reference; and the
cross-cutting guarantees — secrets by reference only (R6), deterministic output
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

# plugins/ live at <repo>/plugins; this test file is <repo>/tests/test_plugin_aws_glue.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"

# The settings key the core threads for a promotion pipeline (design §11.6).
_PROMO_KEY = "promotion_artifact_ref"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_target_platform_plugin("aws-glue", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    """A representative aws-glue IR (GitHub + AWS Glue, OIDC)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": "aws-glue",
            "project": {"name": "demo-etl"},
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
    assert plugin.name == "aws-glue"


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
    assert "glue-extra-python-libs.zip" in text
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


def test_build_emits_committed_glue_job_spec() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    paths = [f.path for f in result.files]
    assert "glue/glue-job.json" in paths
    spec = next(f for f in result.files if f.path == "glue/glue-job.json").content
    # a Spark/Python glue job spec — no image anywhere
    assert "glueetl" in spec
    assert "ScriptLocation" in spec
    assert "docker" not in spec.lower()


def test_build_libs_archive_is_reproducible_flag() -> None:
    # -X strips extra attributes so the archive bytes do not depend on host uid/gid.
    text = _joined(_plugin().build(_ctx(StageVerb.BUILD)))
    assert "zip -r -X" in text


# ===========================================================================
# package -> upload to S3, capture immutable object version (promotion)
# ===========================================================================
def test_package_uploads_scripts_to_s3() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "aws s3api put-object" in text
    # no image push on the data-job path
    assert "docker push" not in text


def test_package_without_promotion_does_not_resolve_a_version() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    # no promotion ref threaded -> no object-version resolution / export
    assert "head-object" not in text
    assert "versionId=" not in text


def test_package_with_promotion_exports_immutable_version_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    # resolves the immutable S3 object version and exports the shared reference
    assert "head-object" in text
    assert "VersionId" in text
    assert "export PROMOTION_ARTIFACT_REF=" in text
    assert "versionId=$OBJECT_VERSION" in text


# ===========================================================================
# deploy -> create/update glue job; reference pre-built ref on promotion
# ===========================================================================
def test_deploy_creates_or_updates_the_glue_job() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "aws glue create-job" in text
    assert "aws glue update-job" in text


def test_deploy_with_promotion_references_prebuilt_reference() -> None:
    settings = {_PROMO_KEY: "$PROMOTION_ARTIFACT_REF"}
    text = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    # consumes/announces the shared pre-built reference; does not re-upload a script
    assert "$PROMOTION_ARTIFACT_REF" in text
    assert "aws s3api put-object" not in text


# ===========================================================================
# verify -> start job run + poll to SUCCEEDED
# ===========================================================================
def test_verify_starts_job_run_and_polls_state() -> None:
    text = _joined(_plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod")))
    assert "aws glue start-job-run" in text
    assert "aws glue get-job-run" in text
    assert "SUCCEEDED" in text


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
        "job_name": "demo-job",
        "script_bucket": "demo-bucket",
        "script_key": "glue/scripts/demo.py",
        "extra_libs_key": "glue/libs/demo.zip",
    }
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    assert "demo-bucket" in pkg
    assert "glue/libs/demo.zip" in pkg
    deploy = _joined(
        _plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod", settings=settings))
    )
    assert "demo-job" in deploy


# ===========================================================================
# supply_iac -> Terraform mentioning Glue job / S3 / IAM / OIDC and the region
# ===========================================================================
def test_supply_iac_returns_terraform_files_under_platform_dir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    assert paths and all(p.startswith("iac/aws-glue/") for p in paths)


def test_iac_mentions_glue_job_s3_iam_and_oidc() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "aws_glue_job" in blob
    assert "aws_s3_bucket" in blob
    assert "aws_iam_role" in blob
    assert "aws_iam_openid_connect_provider" in blob


def test_iac_references_region_from_ir() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "eu-west-2" in blob


def test_iac_summary_describes_what_it_provisions() -> None:
    summary = _plugin().supply_iac(_pipeline()).summary.lower()
    assert "glue" in summary
    assert "oidc" in summary or "iam" in summary


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
    # account specifics are referenced by shell var when not set via settings
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "$GLUE_SCRIPT_BUCKET" in pkg


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
