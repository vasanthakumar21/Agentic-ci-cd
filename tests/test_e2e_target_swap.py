"""E2E demo B — target-platform swap with the SAME CI vendor (task 11.4, R8/R3.5).

Task 11.1 gave the generator a first-class axis-swap signal and manifest-driven
cleanup; task 11.2 turned the signal into a committed ``MIGRATION_RUNBOOK.md``.
Demo A (task 11.3) swapped the CI vendor. This is the complementary proof: keep
the CI vendor FIXED (``github``) and flip ONLY ``target_platform`` from
``aws-ecs`` to ``databricks``, regenerating into the SAME repo, and confirm the
deploy SHAPE changes correctly from a container **image** to a Databricks **job**.

Everything runs against the **REAL** on-disk plugins (``<repo>/plugins``) — no
fakes, no injected registry — so this is exactly what a client would get:

    github × aws-ecs    -> Dockerfile + iac/aws-ecs/* (the IMAGE shape)
    github × databricks -> databricks/job.json + iac/databricks/* (the JOB shape,
                           NO Dockerfile)

Proves, generating into pytest's ``tmp_path`` (never the product repo):

    * first gen (github × aws-ecs): a Dockerfile exists, iac/aws-ecs/main.tf
      exists, the workflow carries the aws image/ECS deploy expression, and the
      manifest records aws-ecs;
    * after flipping target_platform to databricks and regenerating: the
      Dockerfile is REMOVED (and listed in ``result.removed``), iac/aws-ecs/* is
      REMOVED, databricks/job.json + iac/databricks/main.tf appear (NO Dockerfile),
      and the regenerated workflow drops the docker/ECS deploy expression for the
      databricks job verbs;
    * the run is flagged a TARGET-only axis change (target_changed True,
      ci_changed False), ci_vendor stays github in the manifest, and a
      ``MIGRATION_RUNBOOK.md`` naming ``aws-ecs -> databricks`` is emitted;
    * the crisp "deploy shape changed (image -> job)" assertion holds end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cicdgen.agent.demo import run_target_swap_demo
from cicdgen.core import (
    RUNBOOK_RELPATH,
    GenerationResult,
    generate,
    load_manifest,
    write_migration_runbook,
)
from cicdgen.ir import Pipeline

# <repo>/tests/test_e2e_target_swap.py -> <repo>; the REAL plugin tree lives here.
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"

# Shape-defining artifacts for each target platform (design §8b).
DOCKERFILE = "Dockerfile"
JOB_SPEC = "databricks/job.json"
AWS_ECS_IAC_MAIN = "iac/aws-ecs/main.tf"
DBX_IAC_MAIN = "iac/databricks/main.tf"
WORKFLOW = ".github/workflows/ci.yml"


# ---------------------------------------------------------------------------
# IR builders — ci_vendor is ALWAYS github; only target_platform flips.
# ---------------------------------------------------------------------------
def _pipeline(*, target_platform: str) -> Pipeline:
    """A github × <target> IR. CI vendor fixed at github for both runs.

    The cloud block is chosen so the chosen target validates and the GitHub
    plugin emits the matching OIDC step (aws -> aws credentials; azure ->
    azure/login). ``stage.runs`` on build/deploy makes the deploy-shape change
    visible in the generated workflow too — command *references* only, never a
    literal secret value.
    """
    is_ecs = target_platform == "aws-ecs"
    if is_ecs:
        cloud = {"provider": "aws", "region": "eu-west-2", "auth": "oidc"}
        build_runs = "docker build -t $ECR_REPO:$IMAGE_TAG ."
        deploy_runs = "aws ecs update-service --cluster $ECS_CLUSTER --service $ECS_SERVICE"
    else:
        cloud = {"provider": "azure", "region": "uksouth", "auth": "oidc"}
        build_runs = "python -m build --wheel --outdir dist/"
        deploy_runs = "databricks jobs create --json @databricks/job.json"

    return Pipeline.model_validate(
        {
            "version": 1,
            "ci_vendor": "github",
            "target_platform": target_platform,
            "project": {"name": "orders-api", "language": "python"},
            "cloud": cloud,
            "environments": [
                {"name": "dev", "gate": "auto"},
                {"name": "prod", "gate": "manual", "protection": "required-approvals"},
            ],
            "stages": [
                {"id": "test", "runs": "make test", "scans": ["sast", "secrets"]},
                {"id": "build", "runs": build_runs, "timeout_minutes": 30},
                {"id": "deploy-dev", "environment": "dev", "runs": deploy_runs},
            ],
        }
    )


def _gen(target_platform: str, repo: Path) -> GenerationResult:
    result = generate(
        _pipeline(target_platform=target_platform),
        repo,
        plugins_root=PLUGINS_ROOT,
    )
    assert isinstance(result, GenerationResult)
    return result


# ===========================================================================
# The real plugin tree is present (otherwise this whole module is meaningless)
# ===========================================================================
def test_real_plugin_tree_exists() -> None:
    assert (PLUGINS_ROOT / "ci" / "github").is_dir()
    # Target plugins live provider-nested after the §11.1 reorg.
    assert (PLUGINS_ROOT / "target" / "aws" / "ecs").is_dir()
    assert (PLUGINS_ROOT / "target" / "azure" / "databricks").is_dir()


# ===========================================================================
# First gen: github × aws-ecs — the IMAGE shape
# ===========================================================================
def test_first_gen_github_aws_ecs_is_image_shape(tmp_path: Path) -> None:
    result = _gen("aws-ecs", tmp_path)

    # image-shape artifacts present
    assert (tmp_path / DOCKERFILE).is_file()
    assert (tmp_path / AWS_ECS_IAC_MAIN).is_file()
    # and NO job-shape artifacts yet
    assert not (tmp_path / JOB_SPEC).exists()
    assert not (tmp_path / DBX_IAC_MAIN).exists()

    # the workflow carries the aws image/ECS deploy expression
    workflow = (tmp_path / WORKFLOW).read_text(encoding="utf-8")
    assert "aws-actions/configure-aws-credentials" in workflow
    assert "docker build" in workflow
    assert "ecs update-service" in workflow
    # not the azure/databricks expression
    assert "azure/login" not in workflow
    assert "databricks jobs" not in workflow

    # the manifest records the axes; first run is not an axis change
    manifest = load_manifest(tmp_path)
    assert manifest is not None
    assert manifest.ci_vendor == "github"
    assert manifest.target_platform == "aws-ecs"
    assert DOCKERFILE in manifest.paths
    assert JOB_SPEC not in manifest.paths
    assert result.axis_change is None


# ===========================================================================
# Regenerate into the SAME repo with target flipped to databricks — JOB shape
# ===========================================================================
def test_target_swap_changes_deploy_shape_image_to_job(tmp_path: Path) -> None:
    # Arrive at github × aws-ecs first.
    _gen("aws-ecs", tmp_path)
    assert (tmp_path / DOCKERFILE).is_file()
    assert (tmp_path / AWS_ECS_IAC_MAIN).is_file()

    # Flip ONLY target_platform -> databricks (same ci_vendor: github).
    result = _gen("databricks", tmp_path)

    # --- the job-shape artifacts now exist ----------------------------------
    assert (tmp_path / JOB_SPEC).is_file()
    assert (tmp_path / DBX_IAC_MAIN).is_file()

    # --- the image-shape artifacts are REMOVED (and tracked in result.removed)
    assert not (tmp_path / DOCKERFILE).exists()
    assert not (tmp_path / AWS_ECS_IAC_MAIN).exists()
    assert DOCKERFILE in result.removed
    assert AWS_ECS_IAC_MAIN in result.removed
    # every file the aws-ecs IaC module owned is gone (the core removes stale
    # files it owned; an emptied directory may remain, which is harmless).
    assert not any((tmp_path / "iac" / "aws-ecs").glob("*.tf"))
    assert not list((tmp_path / "iac" / "aws-ecs").rglob("*.*"))

    # --- crucially: the JOB shape has NO Dockerfile -------------------------
    assert not (tmp_path / DOCKERFILE).exists(), (
        "databricks is a job, not an image — there must be no Dockerfile"
    )

    # --- the regenerated workflow keeps github but drops docker/ECS ---------
    workflow = (tmp_path / WORKFLOW).read_text(encoding="utf-8")
    assert "docker build" not in workflow
    assert "ecs update-service" not in workflow
    assert "aws-actions/configure-aws-credentials" not in workflow
    # ... and now reflects the databricks verbs / azure OIDC
    assert "databricks jobs" in workflow
    assert "azure/login" in workflow

    # --- flagged as a TARGET-only axis change -------------------------------
    assert result.axis_change is not None
    assert result.axis_change.is_axis_change is True
    assert result.axis_change.target_changed is True
    assert result.axis_change.ci_changed is False
    assert result.axis_change.old_target_platform == "aws-ecs"
    assert result.axis_change.new_target_platform == "databricks"

    # --- ci_vendor stays github in the manifest; target now databricks ------
    manifest = load_manifest(tmp_path)
    assert manifest is not None
    assert manifest.ci_vendor == "github"
    assert manifest.target_platform == "databricks"
    assert JOB_SPEC in manifest.paths
    assert DOCKERFILE not in manifest.paths


# ===========================================================================
# The migration runbook is emitted naming aws-ecs -> databricks
# ===========================================================================
def test_migration_runbook_emitted_for_target_swap(tmp_path: Path) -> None:
    _gen("aws-ecs", tmp_path)
    result = _gen("databricks", tmp_path)

    assert result.axis_change is not None and result.axis_change.is_axis_change

    runbook_path = write_migration_runbook(tmp_path, result.axis_change)
    assert runbook_path == tmp_path / RUNBOOK_RELPATH
    assert runbook_path.is_file()

    body = runbook_path.read_text(encoding="utf-8")
    # names the target swap old -> new
    assert "`aws-ecs` -> `databricks`" in body
    assert "target_platform: aws-ecs -> databricks" in body
    # the CI vendor did not change -> no CI-change line
    assert "CI vendor: `github` -> " not in body
    # the new target's section is present
    assert "### Target platform: `databricks`" in body


# ===========================================================================
# The crisp image -> job proof, end to end
# ===========================================================================
def test_deploy_shape_changed_correctly_image_to_job(tmp_path: Path) -> None:
    # The meaningful signal for "which IaC module is live" is its main.tf file,
    # not an emptied directory the core may leave behind after removing files.
    # BEFORE: a Dockerfile and no job.json; the live IaC module is aws-ecs.
    _gen("aws-ecs", tmp_path)
    before_has_dockerfile = (tmp_path / DOCKERFILE).is_file()
    before_has_job = (tmp_path / JOB_SPEC).is_file()
    before_iac_ecs = (tmp_path / AWS_ECS_IAC_MAIN).is_file()
    before_iac_dbx = (tmp_path / DBX_IAC_MAIN).is_file()
    before_workflow = (tmp_path / WORKFLOW).read_text(encoding="utf-8")

    assert before_has_dockerfile and not before_has_job
    assert before_iac_ecs and not before_iac_dbx

    # AFTER: a job.json and no Dockerfile; the live IaC module is databricks.
    _gen("databricks", tmp_path)
    after_has_dockerfile = (tmp_path / DOCKERFILE).is_file()
    after_has_job = (tmp_path / JOB_SPEC).is_file()
    after_iac_ecs = (tmp_path / AWS_ECS_IAC_MAIN).is_file()
    after_iac_dbx = (tmp_path / DBX_IAC_MAIN).is_file()
    after_workflow = (tmp_path / WORKFLOW).read_text(encoding="utf-8")

    assert after_has_job and not after_has_dockerfile
    assert after_iac_dbx and not after_iac_ecs

    # the shape flipped on every axis of the proof:
    assert (before_has_dockerfile, before_has_job) == (True, False)
    assert (after_has_dockerfile, after_has_job) == (False, True)
    # IaC dir moved from aws-ecs to databricks
    assert (before_iac_ecs, before_iac_dbx) == (True, False)
    assert (after_iac_ecs, after_iac_dbx) == (False, True)
    # the workflow's deploy expression changed from image/ECS to databricks job
    assert "docker build" in before_workflow and "ecs update-service" in before_workflow
    assert "databricks jobs" in after_workflow
    assert "docker build" not in after_workflow and "ecs update-service" not in after_workflow


# ===========================================================================
# The workflow stays valid YAML across the swap and keeps the same CI vendor
# ===========================================================================
def test_workflow_stays_valid_github_yaml_across_swap(tmp_path: Path) -> None:
    _gen("aws-ecs", tmp_path)
    _gen("databricks", tmp_path)

    wf = yaml.safe_load((tmp_path / WORKFLOW).read_text(encoding="utf-8"))
    assert isinstance(wf, dict)
    # still a GitHub Actions workflow (jobs mapping, `on` as a string key)
    assert isinstance(wf.get("jobs"), dict) and wf["jobs"]
    assert "on" in wf and True not in wf


# ===========================================================================
# The runnable demo helper drives the whole swap too (smoke test)
# ===========================================================================
def test_run_target_swap_demo_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "swap-out"
    before, after = run_target_swap_demo(out_dir, plugins_root=PLUGINS_ROOT)

    # the two phases produced the two shapes
    assert before.target_platform == "aws-ecs"
    assert after.target_platform == "databricks"
    assert before.ci_vendor == after.ci_vendor == "github"

    # final on-disk shape is the job shape, Dockerfile gone
    assert (out_dir / JOB_SPEC).is_file()
    assert not (out_dir / DOCKERFILE).exists()
    assert (out_dir / RUNBOOK_RELPATH).is_file()

    # it printed a readable before/after highlighting the shape change
    out = capsys.readouterr().out
    assert "TARGET-PLATFORM SWAP" in out
    assert "BEFORE" in out and "AFTER" in out
    assert "IMAGE" in out and "JOB" in out
