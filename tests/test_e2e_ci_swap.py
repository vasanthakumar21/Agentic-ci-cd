"""E2E demo A — CI-vendor swap, SAME target (task 11.3, R8 / R3.5).

The headline migration: a client on ``github × aws-ecs`` moves their pipeline to
Azure DevOps but keeps deploying to the SAME target (AWS ECS). We flip ONE axis
(``ci_vendor: github -> azure-devops``) and regenerate into the SAME repo through
the REAL on-disk plugins (no fakes, no network), and prove:

    * First gen (github × aws-ecs): ``.github/workflows/ci.yml`` exists and parses
      as YAML with jobs; ``Dockerfile`` + ``iac/aws-ecs/main.tf`` exist; the
      manifest records github × aws-ecs.
    * After the swap (azure-devops × aws-ecs): ``azure-pipelines.yml`` exists and
      parses as YAML with stages; ``.github/workflows/ci.yml`` is REMOVED (and in
      ``result.removed``); the aws-ecs target files are BYTE-IDENTICAL to before
      the swap (captured bytes compared) — proving the target/deploy wiring is
      preserved; ``result.axis_change`` is an axis change with ``ci_changed`` True
      and ``target_changed`` False; the manifest now records azure-devops ×
      aws-ecs; ``MIGRATION_RUNBOOK.md`` is emitted and names github -> azure-devops.
    * "Equivalent pipeline": both vendor files express the SAME ordered stage set
      from the IR (github jobs/needs vs. azure stages/dependsOn), both gate the
      manual prod stage (github ``environment: prod`` / azure deployment-job
      ``environment: prod``), and both reference secrets by reference only.

Everything is driven by the committed sample IR
``examples/client-github-aws/pipeline.yaml`` (ci_vendor flipped in-memory for the
second run), through the real ``github`` / ``azure-devops`` / ``aws-ecs`` plugins.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from cicdgen.agent.demo import run_ci_swap_demo
from cicdgen.core import GenerationResult, generate, load_manifest, write_migration_runbook
from cicdgen.ir import Pipeline

# <repo>/tests/test_e2e_ci_swap.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"
SAMPLE_IR = REPO_ROOT / "examples" / "client-github-aws" / "pipeline.yaml"

GITHUB_WORKFLOW = ".github/workflows/ci.yml"
AZURE_PIPELINE = "azure-pipelines.yml"

#: Files the aws-ecs target plugin owns — must survive a CI-only swap unchanged.
AWS_ECS_TARGET_FILES: tuple[str, ...] = (
    "Dockerfile",
    "iac/aws-ecs/main.tf",
    "iac/aws-ecs/outputs.tf",
    "iac/aws-ecs/variables.tf",
    "iac/aws-ecs/terraform.tfvars.example",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pipeline(*, ci_vendor: str) -> Pipeline:
    """Load the sample IR and set ``ci_vendor`` (target stays ``aws-ecs``)."""
    data = dict(yaml.safe_load(SAMPLE_IR.read_text(encoding="utf-8")))
    data["ci_vendor"] = ci_vendor
    return Pipeline.model_validate(data)


def _gen(pipeline: Pipeline, repo: Path) -> GenerationResult:
    result = generate(pipeline, repo, plugins_root=PLUGINS_ROOT)
    assert isinstance(result, GenerationResult)
    return result


def _capture(repo: Path, rel_paths: tuple[str, ...]) -> dict[str, bytes]:
    return {
        rel: (repo / rel).read_bytes()
        for rel in rel_paths
        if (repo / rel).is_file()
    }


def _azure_stage_name(stage_id: str) -> str:
    """Mirror the azure-devops plugin's id sanitisation (``-`` -> ``_``)."""
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in stage_id)
    if not cleaned or not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"s_{cleaned}"
    return cleaned


def _ir_stage_ids(pipeline: Pipeline) -> list[str]:
    return [s.id for s in pipeline.stages if s.enabled]


# ===========================================================================
# First gen: github × aws-ecs
# ===========================================================================
def test_first_gen_github_aws_ecs_shape(tmp_path: Path) -> None:
    result = _gen(_pipeline(ci_vendor="github"), tmp_path)

    # GitHub workflow exists and parses as YAML with jobs.
    wf_path = tmp_path / GITHUB_WORKFLOW
    assert wf_path.is_file()
    wf = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
    assert isinstance(wf, dict)
    assert isinstance(wf["jobs"], dict) and wf["jobs"]

    # aws-ecs target files exist.
    assert (tmp_path / "Dockerfile").is_file()
    assert (tmp_path / "iac/aws-ecs/main.tf").is_file()

    # Manifest records github × aws-ecs.
    manifest = load_manifest(tmp_path)
    assert manifest is not None
    assert manifest.ci_vendor == "github"
    assert manifest.target_platform == "aws-ecs"
    assert GITHUB_WORKFLOW in manifest.paths

    # First run has no prior manifest -> not an axis change.
    assert result.axis_change is None


# ===========================================================================
# After the swap: azure-devops × aws-ecs
# ===========================================================================
def test_ci_swap_writes_azure_removes_github(tmp_path: Path) -> None:
    _gen(_pipeline(ci_vendor="github"), tmp_path)
    result = _gen(_pipeline(ci_vendor="azure-devops"), tmp_path)

    # Azure pipeline exists and parses as YAML with stages.
    az_path = tmp_path / AZURE_PIPELINE
    assert az_path.is_file()
    az = yaml.safe_load(az_path.read_text(encoding="utf-8"))
    assert isinstance(az, dict)
    assert isinstance(az["stages"], list) and az["stages"]

    # GitHub workflow removed from disk and recorded in result.removed.
    assert not (tmp_path / GITHUB_WORKFLOW).exists()
    assert GITHUB_WORKFLOW in result.removed


def test_ci_swap_target_files_byte_identical(tmp_path: Path) -> None:
    _gen(_pipeline(ci_vendor="github"), tmp_path)
    before = _capture(tmp_path, AWS_ECS_TARGET_FILES)
    # All target files were actually produced on the first gen.
    assert set(before) == set(AWS_ECS_TARGET_FILES)

    result = _gen(_pipeline(ci_vendor="azure-devops"), tmp_path)

    after = _capture(tmp_path, AWS_ECS_TARGET_FILES)
    assert after == before, "aws-ecs target files must be byte-identical across a CI-only swap"
    # None of the target files were removed (the target axis did not change).
    for rel in AWS_ECS_TARGET_FILES:
        assert rel not in result.removed


def test_ci_swap_axis_change_ci_only(tmp_path: Path) -> None:
    _gen(_pipeline(ci_vendor="github"), tmp_path)
    result = _gen(_pipeline(ci_vendor="azure-devops"), tmp_path)

    assert result.axis_change is not None
    assert result.axis_change.is_axis_change is True
    assert result.axis_change.ci_changed is True
    assert result.axis_change.target_changed is False
    assert result.axis_change.old_ci_vendor == "github"
    assert result.axis_change.new_ci_vendor == "azure-devops"
    assert result.axis_change.old_target_platform == "aws-ecs"
    assert result.axis_change.new_target_platform == "aws-ecs"


def test_ci_swap_manifest_records_new_axes(tmp_path: Path) -> None:
    _gen(_pipeline(ci_vendor="github"), tmp_path)
    _gen(_pipeline(ci_vendor="azure-devops"), tmp_path)

    manifest = load_manifest(tmp_path)
    assert manifest is not None
    assert manifest.ci_vendor == "azure-devops"
    assert manifest.target_platform == "aws-ecs"
    assert AZURE_PIPELINE in manifest.paths
    assert GITHUB_WORKFLOW not in manifest.paths


def test_ci_swap_emits_migration_runbook(tmp_path: Path) -> None:
    _gen(_pipeline(ci_vendor="github"), tmp_path)
    result = _gen(_pipeline(ci_vendor="azure-devops"), tmp_path)

    assert result.axis_change is not None
    runbook = write_migration_runbook(tmp_path, result.axis_change)
    assert runbook == tmp_path / "MIGRATION_RUNBOOK.md"
    assert runbook.is_file()
    body = runbook.read_text(encoding="utf-8")
    # names the github -> azure-devops swap (both the summary and the old->new line)
    assert "`github` -> `azure-devops`" in body
    assert "ci_vendor: github -> azure-devops" in body


# ===========================================================================
# Equivalent pipeline — same ordered stage set, same manual gate, secrets by ref
# ===========================================================================
def test_equivalent_pipeline_same_stage_set(tmp_path: Path) -> None:
    gh_pipeline = _pipeline(ci_vendor="github")
    _gen(gh_pipeline, tmp_path)
    gh = yaml.safe_load((tmp_path / GITHUB_WORKFLOW).read_text(encoding="utf-8"))
    gh_jobs = list(gh["jobs"].keys())

    az_pipeline = _pipeline(ci_vendor="azure-devops")
    _gen(az_pipeline, tmp_path)
    az = yaml.safe_load((tmp_path / AZURE_PIPELINE).read_text(encoding="utf-8"))
    az_stages = [s["stage"] for s in az["stages"]]

    ir_stage_ids = _ir_stage_ids(gh_pipeline)

    # Every IR stage id appears in BOTH vendor files, in declared order.
    gh_order = [j for j in gh_jobs if j in ir_stage_ids]
    az_order = [_azure_stage_name(s) for s in ir_stage_ids]
    az_present = [s for s in az_stages if s in az_order]
    assert gh_order == ir_stage_ids
    assert az_present == az_order

    # Both additionally carry the synthetic security stage (scans requested).
    assert "security" in gh_jobs
    assert "security" in az_stages


def test_equivalent_pipeline_manual_prod_gate(tmp_path: Path) -> None:
    _gen(_pipeline(ci_vendor="github"), tmp_path)
    gh = yaml.safe_load((tmp_path / GITHUB_WORKFLOW).read_text(encoding="utf-8"))

    _gen(_pipeline(ci_vendor="azure-devops"), tmp_path)
    az = yaml.safe_load((tmp_path / AZURE_PIPELINE).read_text(encoding="utf-8"))

    # GitHub: the prod job is gated by a 'prod' environment reference.
    assert gh["jobs"]["deploy-prod"]["environment"] == "prod"

    # Azure: the prod stage uses a deployment job bound to the 'prod' environment.
    prod_stage = next(s for s in az["stages"] if s["stage"] == "deploy_prod")
    prod_job = prod_stage["jobs"][0]
    assert "deployment" in prod_job
    assert prod_job["environment"] == "prod"


def test_equivalent_pipeline_secrets_by_reference_only(tmp_path: Path) -> None:
    _gen(_pipeline(ci_vendor="github"), tmp_path)
    gh_text = (tmp_path / GITHUB_WORKFLOW).read_text(encoding="utf-8")

    _gen(_pipeline(ci_vendor="azure-devops"), tmp_path)
    az_text = (tmp_path / AZURE_PIPELINE).read_text(encoding="utf-8")

    # GitHub references secrets as ${{ secrets.NAME }}; Azure via a variable group.
    assert "${{ secrets." in gh_text
    assert "group: cicd-secrets" in az_text
    # Neither file contains a literal credential (e.g. an IAM ARN value).
    assert "arn:aws:iam" not in gh_text
    assert "arn:aws:iam" not in az_text


# ===========================================================================
# The runnable demo function exercises the whole flow end to end
# ===========================================================================
def test_run_ci_swap_demo_end_to_end(tmp_path: Path) -> None:
    repo = tmp_path / "ci-swap"
    result = run_ci_swap_demo(repo, plugins_root=PLUGINS_ROOT)

    assert isinstance(result, GenerationResult)
    assert result.axis_change is not None
    assert result.axis_change.ci_changed is True
    assert result.axis_change.target_changed is False

    # The demo left a coherent repo: azure pipeline present, github removed,
    # target files + runbook present.
    assert (repo / AZURE_PIPELINE).is_file()
    assert not (repo / GITHUB_WORKFLOW).exists()
    assert (repo / "Dockerfile").is_file()
    assert (repo / "iac/aws-ecs/main.tf").is_file()
    assert (repo / "MIGRATION_RUNBOOK.md").is_file()

    manifest = load_manifest(repo)
    assert manifest is not None
    assert manifest.ci_vendor == "azure-devops"
    assert manifest.target_platform == "aws-ecs"
