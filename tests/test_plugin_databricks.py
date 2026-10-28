"""Unit tests for the Databricks target-platform plugin (task 9.1, R4.2/§8b).

Databricks is the **different-shape proof**: the shape MOST unlike aws-ecs. These
tests load the real ``plugins/target/databricks`` package via the loader (no mocks,
no stand-in plugin), call each verb, and assert the design §8b databricks mapping:

    * build   -> assemble a wheel (``python -m build``) + stage notebooks, NO image;
    * package -> upload to DBFS / a Unity Catalog Volume (``databricks fs cp``);
    * deploy  -> create/update a Databricks JOB via the CLI (``databricks jobs``);
    * verify  -> run the job + poll its run state (``run-now`` / ``get-run``);
    * lint/test are reasonable/generic.

The CRITICAL contrast assertions compare the databricks SHAPE against the aws-ecs
plugin loaded side-by-side: NO databricks verb emits ``docker build`` / ``docker
push`` or references ECR/ECS, and ``supply_iac`` emits ``databricks_job`` (not
``aws_ecs_*``). Plus the cross-cutting guarantees — secrets by reference only
(``$DATABRICKS_HOST`` / ``$DATABRICKS_TOKEN``, no literals; R6) and deterministic
output (R5.3).
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

# plugins/ live at <repo>/plugins; this file is <repo>/tests/test_plugin_databricks.py
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _plugin():
    return load_target_platform_plugin("databricks", plugins_root=PLUGINS_ROOT)


def _aws_ecs_plugin():
    return load_target_platform_plugin("aws-ecs", plugins_root=PLUGINS_ROOT)


def _pipeline() -> Pipeline:
    """A representative databricks IR (Azure DevOps + Databricks, no image)."""
    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "azure-devops",
            "target_platform": "databricks",
            "project": {"name": "demo-notebooks"},
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


# ===========================================================================
# Loader / identity
# ===========================================================================
def test_plugin_loads_by_name() -> None:
    assert _plugin().name == "databricks"


def test_all_six_verbs_dispatch_via_run_verb() -> None:
    plugin = _plugin()
    for verb in StageVerb:
        result = plugin.run_verb(_ctx(verb))
        assert result.commands, f"verb {verb} produced no commands"


# ===========================================================================
# build -> wheel/notebooks, NO image
# ===========================================================================
def test_build_assembles_a_wheel() -> None:
    text = _joined(_plugin().build(_ctx(StageVerb.BUILD)))
    assert "python -m build" in text
    assert ".whl" in text or "dist/" in text


def test_build_stages_notebooks() -> None:
    text = _joined(_plugin().build(_ctx(StageVerb.BUILD)))
    assert "notebooks" in text.lower()


def test_build_emits_a_job_spec_support_file() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    paths = [f.path for f in result.files]
    assert any(p.endswith("job.json") for p in paths)


def test_build_does_not_emit_a_dockerfile() -> None:
    result = _plugin().build(_ctx(StageVerb.BUILD))
    paths = [f.path.lower() for f in result.files]
    assert not any("dockerfile" in p for p in paths)


# ===========================================================================
# package -> upload to DBFS/Volumes (not an image push)
# ===========================================================================
def test_package_uploads_to_dbfs_or_volume() -> None:
    text = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "databricks fs cp" in text or "databricks workspace import" in text


# ===========================================================================
# deploy -> create/update a Databricks job via CLI
# ===========================================================================
def test_deploy_creates_or_updates_a_databricks_job() -> None:
    text = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    assert "databricks jobs" in text
    assert "reset" in text or "create" in text


# ===========================================================================
# verify -> run the job + check run state
# ===========================================================================
def test_verify_runs_job_and_checks_run_state() -> None:
    text = _joined(_plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod")))
    assert "databricks jobs run-now" in text
    assert "get-run" in text
    # asserts the terminal result is SUCCESS
    assert "SUCCESS" in text


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
        "artifact_path": "dbfs:/FileStore/artifacts/demo.whl",
        "job_name": "demo-job",
        "job_id": "4242",
    }
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE, settings=settings)))
    assert "dbfs:/FileStore/artifacts/demo.whl" in pkg
    verify = _joined(
        _plugin().verify(_ctx(StageVerb.VERIFY, stage_id="deploy-prod", settings=settings))
    )
    assert "--job-id 4242" in verify


# ===========================================================================
# supply_iac -> databricks Terraform (databricks_job), NOT aws_ecs
# ===========================================================================
def test_supply_iac_returns_terraform_files() -> None:
    iac = _plugin().supply_iac(_pipeline())
    paths = [f.path for f in iac.files]
    assert any(p.endswith("main.tf") for p in paths)
    assert any(p.endswith("variables.tf") for p in paths)
    assert paths and all(p.startswith("iac/databricks/") for p in paths)


def test_iac_emits_databricks_job_and_unity_catalog() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    assert "databricks_job" in blob
    assert "databricks_schema" in blob
    # uses the official databricks provider
    assert "databricks/databricks" in blob


def test_iac_summary_describes_what_it_provisions() -> None:
    summary = _plugin().supply_iac(_pipeline()).summary.lower()
    assert "job" in summary
    assert "databricks" in summary


# ===========================================================================
# CRITICAL CONTRAST: databricks SHAPE is different from aws-ecs
# ===========================================================================
def test_no_verb_emits_docker_or_references_ecr_ecs() -> None:
    """The whole point: databricks has NO image, NO ECR, NO ECS anywhere.

    The only permitted occurrences of "ECR"/"ECS" are the summary's explicit
    *negations* ("NO ECR, and NO ECS") — those are the proof, not a reference. So
    we strip the summary's negated mentions, then assert no real command/resource
    references ECR, ECS, or Docker survive.
    """
    blob = _all_generated_text(_plugin())
    # drop the explicit "NO ECR"/"NO ECS" negations and the "aws-ecs" contrast
    # mentions (prose documenting the proof) before the substring checks
    blob = re.sub(r"NO EC[RS]", "", blob)
    blob = blob.replace("aws-ecs", "")
    lowered = blob.lower()
    assert "docker build" not in lowered
    assert "docker push" not in lowered
    assert "docker login" not in lowered
    # word-boundary so "secret"/"recreate" etc. don't false-positive on "ecr"/"ecs"
    assert not re.search(r"\becr\b", lowered)
    assert not re.search(r"\becs\b", lowered)
    assert "aws ecs" not in lowered
    assert "aws_ecs" not in lowered
    assert "register-task-definition" not in lowered


def test_shape_differs_from_aws_ecs_side_by_side() -> None:
    """Load BOTH plugins and assert the verb shapes diverge the §8b way."""
    db = _plugin()
    ecs = _aws_ecs_plugin()

    ecs_build = _joined(ecs.build(_ctx(StageVerb.BUILD)))
    db_build = _joined(db.build(_ctx(StageVerb.BUILD)))
    # aws-ecs build is docker; databricks build is a wheel.
    assert "docker build" in ecs_build
    assert "docker build" not in db_build
    assert "python -m build" in db_build

    ecs_pkg = _joined(ecs.package(_ctx(StageVerb.PACKAGE)))
    db_pkg = _joined(db.package(_ctx(StageVerb.PACKAGE)))
    # aws-ecs package pushes an image; databricks uploads to DBFS/Volumes.
    assert "docker push" in ecs_pkg
    assert "docker push" not in db_pkg
    assert "databricks fs cp" in db_pkg or "databricks workspace import" in db_pkg

    ecs_deploy = _joined(ecs.deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    db_deploy = _joined(db.deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    # aws-ecs deploy rolls an ECS service; databricks creates/updates a job.
    assert "aws ecs update-service" in ecs_deploy
    assert "databricks jobs" in db_deploy
    assert "aws ecs" not in db_deploy


def test_iac_shape_differs_from_aws_ecs() -> None:
    """aws-ecs IaC is aws_ecs_*; databricks IaC is databricks_job — not ECS."""
    db_blob = "\n".join(f.content for f in _plugin().supply_iac(_pipeline()).files)
    ecs_blob = "\n".join(f.content for f in _aws_ecs_plugin().supply_iac(_pipeline()).files)
    assert "aws_ecs_service" in ecs_blob
    assert "databricks_job" in db_blob
    assert "aws_ecs" not in db_blob
    assert "aws_ecr_repository" not in db_blob
    assert "databricks_job" not in ecs_blob


# ===========================================================================
# Secrets by reference only (R6) — host/token are env refs, never literals
# ===========================================================================
def test_host_and_token_are_env_references() -> None:
    """The CLI's env contract is referenced; no workspace URL / token literal."""
    blob = _all_generated_text(_plugin())
    # host/token referenced by env in the IaC (variables, not values) + verbs.
    assert "DATABRICKS_HOST" in blob or "databricks_host" in blob
    assert "DATABRICKS_TOKEN" in blob or "databricks_token" in blob


def test_no_literal_workspace_url_or_token() -> None:
    """No real workspace URL/token is *assigned* in committed output (R6).

    Placeholder URLs that appear in a TF ``description = "..."`` or on a commented
    (``#``) example line are documentation, not committed values — those are
    allowed (and expected). What must never appear is a live assignment of a real
    host/token to an active variable. We check every non-comment line for a bare
    ``host``/``token`` assignment to a concrete ``https://...databricks.com`` URL
    or a token literal, and confirm the host/token are referenced instead.
    """
    blob = _all_generated_text(_plugin())
    for line in blob.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "description" in stripped:
            continue  # comments + TF description examples are documentation only
        # an *active* assignment of a concrete workspace URL is forbidden
        assert not re.search(
            r"(?i)(host|url)\s*[:=]\s*\"https://[a-z0-9-]+\.cloud\.databricks\.com",
            stripped,
        ), f"literal workspace URL assigned: {stripped!r}"
        # an *active* assignment of a token literal is forbidden
        assert not re.search(
            r"(?i)\btoken\s*[:=]\s*\"?(dapi|dkea)[A-Za-z0-9]+", stripped
        ), f"literal token assigned: {stripped!r}"
    # host/token are referenced via the CLI/TF variable contract instead
    assert "var.databricks_host" in blob
    assert "var.databricks_token" in blob
    # the artifact path is referenced when not set via settings
    pkg = _joined(_plugin().package(_ctx(StageVerb.PACKAGE)))
    assert "$DATABRICKS_ARTIFACT_PATH" in pkg


def test_token_variable_is_marked_sensitive() -> None:
    iac = _plugin().supply_iac(_pipeline())
    blob = "\n".join(f.content for f in iac.files)
    # the token TF variable must be sensitive so it is never surfaced (R6)
    assert "sensitive   = true" in blob or "sensitive = true" in blob


# ===========================================================================
# Task 9.2 — workspace + job + OPTIONAL Unity Catalog wiring, IR-parameterized
# ===========================================================================
def _iac_files() -> dict[str, str]:
    """Freshly generate the databricks IaC keyed by basename."""
    iac = _plugin().supply_iac(_pipeline())
    return {Path(f.path).name: f.content for f in iac.files}


def test_iac_emits_the_full_module_including_readme() -> None:
    """9.2 deepens the module to a reviewable unit with a README (Terraform/API)."""
    files = _iac_files()
    for name in ("main.tf", "variables.tf", "outputs.tf", "terraform.tfvars.example"):
        assert name in files, f"missing {name}"
    assert "README.md" in files, "module should ship a README documenting the split"


def test_each_tf_file_passes_structural_validator() -> None:
    """Every .tf file must pass cicdgen.validate.validate_hcl_structure (9.2)."""
    from cicdgen.validate import validate_hcl_structure

    files = _iac_files()
    for name in ("main.tf", "variables.tf", "outputs.tf"):
        validate_hcl_structure(files[name], filename=name)  # raises on malformed HCL


def test_provider_wires_workspace_host_and_token_by_reference() -> None:
    """Workspace wiring: provider host/token come from vars, token sensitive (R6)."""
    main = _iac_files()["main.tf"]
    variables = _iac_files()["variables.tf"]
    assert 'provider "databricks"' in main
    assert "host  = var.databricks_host" in main
    assert "token = var.databricks_token" in main
    # the token variable is sensitive and the host var carries no literal URL
    assert "sensitive   = true" in variables or "sensitive = true" in variables
    assert ".cloud.databricks.com" not in main  # no literal workspace URL in main


def test_job_references_artifact_path_variable() -> None:
    """Job wiring: the databricks_job runs the uploaded artifact by reference."""
    main = _iac_files()["main.tf"]
    assert "databricks_job" in main
    assert "whl = var.artifact_path" in main


def test_job_name_parameterized_by_ir_project() -> None:
    """Naming is IR-driven: the job name derives from the project name."""
    main = _iac_files()["main.tf"]
    # _pipeline() project is 'demo-notebooks' -> prefix 'demo-notebooks'
    assert 'name_prefix = "demo-notebooks"' in main
    assert "${local.name_prefix}-job" in main


def test_unity_catalog_is_optional_gated_by_a_variable() -> None:
    """OPTIONAL Unity Catalog: the schema is gated by a bool var via count (9.2)."""
    main = _iac_files()["main.tf"]
    variables = _iac_files()["variables.tf"]
    # a bool flag variable exists, defaulting to the opt-in true
    assert 'variable "enable_unity_catalog"' in variables
    assert "type        = bool" in variables
    # the schema resource is conditional on the flag (count), not unconditional
    assert "count        = var.enable_unity_catalog ? 1 : 0" in main
    assert "databricks_schema" in main


def test_unity_catalog_output_is_null_when_disabled() -> None:
    """The UC schema output must tolerate the schema being absent (count=0)."""
    outputs = _iac_files()["outputs.tf"]
    # references the count-indexed schema and falls back to null when disabled
    assert "var.enable_unity_catalog ?" in outputs
    assert "databricks_schema.app[0].name" in outputs


def test_job_schedule_and_run_as_are_optional() -> None:
    """Schedule + run-as are IR/config-driven and optional (empty => omitted)."""
    main = _iac_files()["main.tf"]
    variables = _iac_files()["variables.tf"]
    assert 'variable "job_schedule_cron"' in variables
    assert 'variable "run_as_user_name"' in variables
    # rendered as dynamic blocks that disappear when the var is empty
    assert 'dynamic "schedule"' in main
    assert 'dynamic "run_as"' in main


def test_region_is_informational_not_a_secret() -> None:
    """cloud.region flows in as a non-secret informational default (R6)."""
    variables = _iac_files()["variables.tf"]
    # _pipeline() region is eu-west-2
    assert 'default     = "eu-west-2"' in variables


def test_readme_documents_terraform_vs_cli_split_and_workspace_boundary() -> None:
    """9.2 Terraform/API: README explains the split + is honest about limits (R7.1)."""
    readme = _iac_files()["README.md"].lower()
    # Terraform provisions job + UC; CLI verbs upload + run the job
    assert "terraform" in readme and "cli" in readme
    assert "package" in readme and "verify" in readme
    # honest workspace boundary: creating the workspace is an account-level step
    assert "account-level" in readme or "account level" in readme
    assert "does not create" in readme


def test_terraform_and_cli_share_job_name_convention() -> None:
    """Consistency: the TF job name and the deploy verb's job name agree (9.2)."""
    main = _iac_files()["main.tf"]
    deploy = _joined(_plugin().deploy(_ctx(StageVerb.DEPLOY, stage_id="deploy-prod")))
    # both converge on '<project>-job' — TF via local.name_prefix, verb via settings
    assert "${local.name_prefix}-job" in main
    assert "demo-notebooks-job" in deploy


def test_no_account_or_workspace_literals_in_committed_module() -> None:
    """No literal workspace URL / token / account id anywhere in the module (R6)."""
    files = _iac_files()
    for name, content in files.items():
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "description" in stripped:
                continue  # comments + TF descriptions are documentation only
            assert not re.search(
                r"(?i)(host|url)\s*[:=]\s*\"https://[a-z0-9-]+\.cloud\.databricks\.com",
                stripped,
            ), f"{name}: literal workspace URL assigned: {stripped!r}"
            assert not re.search(
                r"(?i)\btoken\s*[:=]\s*\"?(dapi|dkea)[A-Za-z0-9]+", stripped
            ), f"{name}: literal token assigned: {stripped!r}"


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
