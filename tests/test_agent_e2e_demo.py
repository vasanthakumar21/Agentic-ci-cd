"""End-to-end demonstration: ONE instruction → the full output (task 10.4).

This is the end-to-end proof of the product's headline claim (R5.1, R5.2, R10.1,
R10.2): a single natural-language instruction naming the two independent axes
(``ci_vendor`` and ``target_platform``) drives the agent to produce the WHOLE
CI/CD setup — through the **REAL** plugins on disk, not fakes.

Unlike ``test_agent_generate.py`` (which injects in-memory fake plugins to unit
test the driver), every test here runs against the actual plugin tree
(``<repo>/plugins``) with **no injected registry**, so the agent exercises the
real ``github`` / ``azure-devops`` CI plugins and the real ``aws-ecs`` /
``databricks`` target plugins — the same code a client would run. Everything is
generated into pytest's ``tmp_path`` (never the product repo), with no network or
account access (R9 isolation).

Proves, from ONE ``run_instruction`` call with a full instruction:

    * both axes resolve with no ``needs_confirmation`` (R10.1/R10.2);
    * ``apply=True`` writes the real GitHub workflow (parses as YAML, has jobs),
      the aws-ecs Dockerfile, the iac/aws-ecs terraform files, and the manifest;
    * the generated workflow references secrets by reference only (no literals);
    * determinism: the same instruction into two temp dirs → byte-identical files
      (R5.3);
    * the agent itself writes nothing in dry-run (a plan only — R10 safety);
    * two-axis generality: the OTHER combination (azure-devops × databricks) from
      one instruction produces its correctly-shaped output (a Databricks job, and
      crucially NO Dockerfile — the "different shape" proof, R3.3/R4.2).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cicdgen.agent import DriverResult, run_instruction
from cicdgen.agent.demo import run_demo
from cicdgen.core import MANIFEST_RELPATH, GenerationPlan, GenerationResult, load_manifest

# <repo>/tests/test_agent_e2e_demo.py -> <repo>; the real plugin tree lives at <repo>/plugins.
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"

GITHUB_AWS_INSTRUCTION = (
    "generate for ci_vendor=github target_platform=aws-ecs "
    "with region=eu-west-2 project=orders-api"
)
AZDO_DATABRICKS_INSTRUCTION = (
    "generate for ci_vendor=azure-devops target_platform=databricks "
    "with project=analytics-jobs"
)

# The aws-ecs IaC files the real target plugin supplies (design §8b).
AWS_ECS_TF_FILES = (
    "iac/aws-ecs/main.tf",
    "iac/aws-ecs/variables.tf",
    "iac/aws-ecs/outputs.tf",
    "iac/aws-ecs/terraform.tfvars.example",
)


# ---------------------------------------------------------------------------
# Helpers — always the REAL plugins (no injected registry)
# ---------------------------------------------------------------------------
def _apply(instruction: str, repo: Path) -> DriverResult:
    """Run one instruction end-to-end (apply) into ``repo`` via the real plugins."""
    return run_instruction(
        instruction,
        repo,
        apply=True,
        use_discovery=False,
        plugins_root=PLUGINS_ROOT,
    )


def _dry_run(instruction: str, repo: Path) -> DriverResult:
    return run_instruction(
        instruction,
        repo,
        apply=False,
        use_discovery=False,
        plugins_root=PLUGINS_ROOT,
    )


def _files_on_disk(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _write(root: Path, rel: str, content: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _github_aws_client_repo(root: Path) -> Path:
    """A realistic client repo whose signals let discovery infer cloud=aws (OIDC).

    The aws Terraform signal is what lets the driver seed ``cloud.provider=aws``
    into the IR, so the real GitHub plugin emits the cloud-OIDC steps that
    reference ``${{ secrets.* }}`` — the positive half of "secrets by reference".
    """
    root.mkdir(parents=True, exist_ok=True)
    _write(root, ".github/workflows/ci.yml", "name: ci\njobs: {}\n")
    _write(root, "Dockerfile", "FROM python:3.12-slim\n")
    _write(root, "infra/main.tf", 'provider "aws" {}\nresource "aws_ecs_service" "s" {}\n')
    _write(root, "pyproject.toml", "[project]\nname='orders-api'\n")
    return root


# ===========================================================================
# The real plugin tree is present (otherwise this whole module is meaningless)
# ===========================================================================
def test_real_plugin_tree_exists() -> None:
    assert (PLUGINS_ROOT / "ci" / "github").is_dir()
    assert (PLUGINS_ROOT / "ci" / "azure-devops").is_dir()
    # Target plugins live provider-nested after the §11.1 reorg.
    assert (PLUGINS_ROOT / "target" / "aws" / "ecs").is_dir()
    assert (PLUGINS_ROOT / "target" / "azure" / "databricks").is_dir()


# ===========================================================================
# ONE instruction resolves BOTH axes — no confirmation needed (R10.1/R10.2)
# ===========================================================================
def test_single_instruction_resolves_both_axes(tmp_path: Path) -> None:
    repo = tmp_path / "orders-api"
    repo.mkdir()
    result = _dry_run(GITHUB_AWS_INSTRUCTION, repo)

    assert isinstance(result, DriverResult)
    assert result.needs_confirmation is False
    assert result.ci_vendor == "github"
    assert result.target_platform == "aws-ecs"
    assert isinstance(result.plan, GenerationPlan)
    assert result.plan.paths, "the plan should list files that would be written"


# ===========================================================================
# The agent writes NOTHING in dry-run — a plan only (R10 safety posture)
# ===========================================================================
def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "orders-api"
    repo.mkdir()
    before = _files_on_disk(repo)

    result = _dry_run(GITHUB_AWS_INSTRUCTION, repo)

    assert result.applied is False
    assert result.result is None
    assert isinstance(result.plan, GenerationPlan)
    # disk is untouched: no generated files, no manifest
    assert _files_on_disk(repo) == before == set()
    assert not (repo / MANIFEST_RELPATH).exists()


# ===========================================================================
# apply=True writes the FULL, REAL output (workflow + Dockerfile + IaC + manifest)
# ===========================================================================
def test_apply_writes_full_real_output(tmp_path: Path) -> None:
    repo = tmp_path / "orders-api"
    repo.mkdir()
    result = _apply(GITHUB_AWS_INSTRUCTION, repo)

    assert result.applied is True
    assert isinstance(result.result, GenerationResult)

    # --- the real GitHub workflow: exists, parses as YAML, has jobs ----------
    workflow_path = repo / ".github" / "workflows" / "ci.yml"
    assert workflow_path.is_file()
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    assert isinstance(workflow.get("jobs"), dict) and workflow["jobs"], "workflow has jobs"
    # `on:` survived as a string key, not the YAML-1.1 boolean True
    assert "on" in workflow and True not in workflow

    # --- the aws-ecs Dockerfile ---------------------------------------------
    assert (repo / "Dockerfile").is_file()

    # --- the iac/aws-ecs terraform module -----------------------------------
    for rel in AWS_ECS_TF_FILES:
        assert (repo / rel).is_file(), f"missing generated IaC file: {rel}"

    # --- the manifest (the generator's memory of what it owns) ---------------
    manifest = load_manifest(repo)
    assert manifest is not None
    assert manifest.ci_vendor == "github"
    assert manifest.target_platform == "aws-ecs"
    assert ".github/workflows/ci.yml" in manifest.paths
    assert "Dockerfile" in manifest.paths
    assert "iac/aws-ecs/main.tf" in manifest.paths
    # the manifest itself was written too
    assert (repo / MANIFEST_RELPATH).is_file()
    assert MANIFEST_RELPATH in result.result.written


# ===========================================================================
# Secrets by reference only — no literal values in the generated workflow (R6.3/R6.6)
# ===========================================================================
def test_workflow_references_secrets_by_reference_only(tmp_path: Path) -> None:
    # A client repo with an aws signal -> discovery seeds cloud.provider=aws so the
    # real GitHub plugin emits the cloud-OIDC steps that reference secrets.
    repo = _github_aws_client_repo(tmp_path / "orders-api")
    result = run_instruction(
        "generate for ci_vendor=github target_platform=aws-ecs with project=orders-api",
        repo,
        apply=True,
        use_discovery=True,
        plugins_root=PLUGINS_ROOT,
    )
    assert result.assembled_ir.get("cloud", {}).get("provider") == "aws"

    content = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # references use the GitHub secrets.* form ...
    assert "${{ secrets." in content, "workflow should reference secrets by key"
    # ... and never inline a literal AWS credential / ARN value.
    assert "arn:aws:iam" not in content
    assert "AKIA" not in content  # no literal access-key id

    # The core's own secret-leak guard also ran (it would have raised on apply);
    # reaching here means the generated files passed it.


# ===========================================================================
# Determinism: the same instruction into two temp dirs → byte-identical files (R5.3)
# ===========================================================================
def test_same_instruction_is_byte_identical_across_runs(tmp_path: Path) -> None:
    repo_a = tmp_path / "run-a"
    repo_b = tmp_path / "run-b"
    repo_a.mkdir()
    repo_b.mkdir()

    _apply(GITHUB_AWS_INSTRUCTION, repo_a)
    _apply(GITHUB_AWS_INSTRUCTION, repo_b)

    files_a = _files_on_disk(repo_a)
    files_b = _files_on_disk(repo_b)
    assert files_a == files_b, "the same instruction produced a different file set"

    for rel in sorted(files_a):
        bytes_a = (repo_a / rel).read_bytes()
        bytes_b = (repo_b / rel).read_bytes()
        assert bytes_a == bytes_b, f"{rel} differs between two identical runs"


# ===========================================================================
# The agent authors nothing itself — every file comes from a plugin/the manifest
# ===========================================================================
def test_agent_authors_no_files_itself(tmp_path: Path) -> None:
    repo = tmp_path / "orders-api"
    repo.mkdir()
    result = _apply(GITHUB_AWS_INSTRUCTION, repo)
    assert result.result is not None

    # The plugins own exactly the manifest.paths; the core additionally writes the
    # manifest file. On-disk must equal that union — nothing hand-authored.
    owned = set(result.result.manifest.paths) | {MANIFEST_RELPATH}
    assert _files_on_disk(repo) == owned


# ===========================================================================
# Dry-run plan == what apply actually writes (plan is honest)
# ===========================================================================
def test_dry_run_plan_matches_applied_output(tmp_path: Path) -> None:
    plan_repo = tmp_path / "plan"
    apply_repo = tmp_path / "apply"
    plan_repo.mkdir()
    apply_repo.mkdir()

    planned = set(_dry_run(GITHUB_AWS_INSTRUCTION, plan_repo).plan.paths)
    applied = _apply(GITHUB_AWS_INSTRUCTION, apply_repo)
    written = set(applied.result.written) - {MANIFEST_RELPATH}

    assert planned == written


# ===========================================================================
# Two-axis generality: the OTHER combination from one instruction (R3.3/R4.2)
# ===========================================================================
def test_other_combination_azdo_databricks_one_instruction(tmp_path: Path) -> None:
    repo = tmp_path / "analytics-jobs"
    repo.mkdir()
    result = _apply(AZDO_DATABRICKS_INSTRUCTION, repo)

    assert result.needs_confirmation is False
    assert result.ci_vendor == "azure-devops"
    assert result.target_platform == "databricks"

    # The Azure DevOps pipeline file exists and parses as YAML.
    pipeline_path = repo / "azure-pipelines.yml"
    assert pipeline_path.is_file()
    azdo = yaml.safe_load(pipeline_path.read_text(encoding="utf-8"))
    assert isinstance(azdo, dict)

    # Databricks is the DIFFERENT shape: a job, NOT a container image.
    assert (repo / "databricks" / "job.json").is_file()
    assert not (repo / "Dockerfile").exists(), (
        "databricks target must NOT emit a Dockerfile — it deploys a job, not an image"
    )

    manifest = load_manifest(repo)
    assert manifest is not None
    assert manifest.ci_vendor == "azure-devops"
    assert manifest.target_platform == "databricks"
    assert "azure-pipelines.yml" in manifest.paths
    assert "Dockerfile" not in manifest.paths


def test_two_combinations_differ_in_deploy_shape(tmp_path: Path) -> None:
    """One instruction each: the aws-ecs combo has a Dockerfile; databricks does not.

    This is the crisp two-axis proof — the SAME one-instruction flow yields
    fundamentally different deploy artifacts depending only on target_platform
    (image build vs Databricks job), with zero vendor logic in the agent.
    """
    aws_repo = tmp_path / "aws"
    dbx_repo = tmp_path / "dbx"
    aws_repo.mkdir()
    dbx_repo.mkdir()

    _apply(GITHUB_AWS_INSTRUCTION, aws_repo)
    _apply(AZDO_DATABRICKS_INSTRUCTION, dbx_repo)

    assert (aws_repo / "Dockerfile").is_file()
    assert not (dbx_repo / "Dockerfile").exists()
    assert (dbx_repo / "databricks" / "job.json").is_file()
    assert not (aws_repo / "databricks" / "job.json").exists()


# ===========================================================================
# The runnable demo helper drives the real end-to-end flow too (smoke test)
# ===========================================================================
def test_demo_run_demo_helper_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "demo-out"
    result = run_demo(GITHUB_AWS_INSTRUCTION, out_dir, plugins_root=PLUGINS_ROOT)

    assert result.applied is True
    assert result.result is not None
    assert (out_dir / ".github" / "workflows" / "ci.yml").is_file()
    assert (out_dir / "Dockerfile").is_file()

    # it printed a readable before/after report
    out = capsys.readouterr().out
    assert "ONE INSTRUCTION:" in out
    assert "FULL output produced" in out
    assert "dry-run wrote 0 files" in out
