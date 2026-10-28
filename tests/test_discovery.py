"""Tests for client stack discovery (task 10.2, R5.1/R5.2, R10.1/R10.2).

Each test builds a fake client repo under ``tmp_path`` with a different
combination of filesystem signals, then asserts that ``discover_stack``:

    * proposes the right ci_vendor x target_platform (+ cloud provider),
    * sets a confidence level per axis and names the triggering file(s) in the
      evidence,
    * yields a draft IR that validates against the schema / Pipeline model when
      all required fields are present (or is clearly marked draft),
    * NEVER writes generated files (propose-only — R10.1).

Signals exercised:
    * .github/workflows + Dockerfile + *.tf(aws) -> github x aws-ecs, aws
    * azure-pipelines.yml + notebooks/*.ipynb     -> azure-devops x databricks
    * .gitlab-ci.yml + k8s Deployment manifest    -> gitlab x kubernetes
    * empty repo                                   -> low-confidence defaults
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from cicdgen.discovery import (
    Confidence,
    StackProposal,
    discover_stack,
    render_proposal,
)
from cicdgen.ir.models import Pipeline

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "ir" / "pipeline.schema.json"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _write(root: Path, rel: str, content: str = "") -> Path:
    """Create a file (and parents) under ``root`` for a fake client repo."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _snapshot(root: Path) -> set[Path]:
    return set(root.rglob("*"))


# ---------------------------------------------------------------------------
# 1. github x aws-ecs x aws
# ---------------------------------------------------------------------------
def test_github_awsecs_aws(tmp_path: Path, schema: dict) -> None:
    repo = tmp_path / "orders-api"
    repo.mkdir()
    _write(repo, ".github/workflows/ci.yml", "name: ci\njobs: {}\n")
    _write(repo, "Dockerfile", "FROM python:3.11\n")
    _write(
        repo,
        "infra/main.tf",
        'provider "aws" {}\nresource "aws_ecs_service" "svc" {}\n',
    )
    _write(repo, "pyproject.toml", "[project]\nname='orders-api'\n")

    before = _snapshot(repo)
    proposal = discover_stack(repo)
    after = _snapshot(repo)

    # R10.1 — discovery wrote nothing.
    assert before == after, "discovery must not create/modify files (propose-only, R10.1)"

    assert proposal.ir["ci_vendor"] == "github"
    assert proposal.ir["target_platform"] == "aws-ecs"
    assert proposal.ir["cloud"]["provider"] == "aws"
    assert proposal.ir["project"]["name"] == "orders-api"
    assert proposal.ir["project"]["language"] == "python"

    ci = proposal.evidence["ci_vendor"]
    assert ci.value == "github"
    assert ci.confidence is Confidence.HIGH
    assert any("workflows/ci.yml" in s for s in ci.signals)

    target = proposal.evidence["target_platform"]
    assert target.value == "aws-ecs"
    assert target.confidence is Confidence.HIGH  # Dockerfile + ECS hint
    assert any("Dockerfile" in s for s in target.signals)

    provider = proposal.evidence["cloud.provider"]
    assert provider.value == "aws"
    assert provider.confidence is Confidence.HIGH
    assert provider.signals  # named the .tf / action file

    # Draft validates against both the Pydantic model and the JSON Schema.
    assert isinstance(proposal.pipeline, Pipeline)
    assert not proposal.is_draft
    jsonschema.validate(instance=proposal.ir, schema=schema)


# ---------------------------------------------------------------------------
# 2. azure-devops x databricks
# ---------------------------------------------------------------------------
def test_azuredevops_databricks(tmp_path: Path, schema: dict) -> None:
    repo = tmp_path / "ml-jobs"
    repo.mkdir()
    _write(repo, "azure-pipelines.yml", "stages: []\n")
    _write(repo, "notebooks/train.ipynb", "{}")
    _write(repo, "pyproject.toml", "[project]\nname='ml-jobs'\n")

    before = _snapshot(repo)
    proposal = discover_stack(repo)
    after = _snapshot(repo)
    assert before == after

    assert proposal.ir["ci_vendor"] == "azure-devops"
    assert proposal.ir["target_platform"] == "databricks"

    ci = proposal.evidence["ci_vendor"]
    assert ci.value == "azure-devops"
    assert ci.confidence is Confidence.HIGH
    assert "azure-pipelines.yml" in ci.signals

    target = proposal.evidence["target_platform"]
    assert target.value == "databricks"
    assert target.confidence in (Confidence.HIGH, Confidence.MEDIUM)
    assert any(".ipynb" in s or "notebooks" in s for s in target.signals)

    # databricks → azure provider default.
    assert proposal.ir["cloud"]["provider"] == "azure"

    assert isinstance(proposal.pipeline, Pipeline)
    jsonschema.validate(instance=proposal.ir, schema=schema)


# ---------------------------------------------------------------------------
# 3. gitlab x kubernetes
# ---------------------------------------------------------------------------
def test_gitlab_kubernetes(tmp_path: Path, schema: dict) -> None:
    repo = tmp_path / "web-svc"
    repo.mkdir()
    _write(repo, ".gitlab-ci.yml", "stages: [test, build]\n")
    _write(
        repo,
        "k8s/deployment.yaml",
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n",
    )
    _write(repo, "go.mod", "module web-svc\n")

    before = _snapshot(repo)
    proposal = discover_stack(repo)
    after = _snapshot(repo)
    assert before == after

    assert proposal.ir["ci_vendor"] == "gitlab"
    assert proposal.ir["target_platform"] == "kubernetes"

    ci = proposal.evidence["ci_vendor"]
    assert ci.value == "gitlab"
    assert ci.confidence is Confidence.HIGH
    assert ".gitlab-ci.yml" in ci.signals

    target = proposal.evidence["target_platform"]
    assert target.value == "kubernetes"
    assert target.confidence is Confidence.HIGH
    assert any("deployment.yaml" in s for s in target.signals)

    assert proposal.evidence["project.language"].value == "go"

    # kubernetes has no single default provider → unresolved cloud, flagged.
    assert "cloud" not in proposal.ir
    assert proposal.evidence["cloud.provider"].value is None
    assert proposal.evidence["cloud.provider"].confidence is Confidence.LOW

    assert isinstance(proposal.pipeline, Pipeline)
    jsonschema.validate(instance=proposal.ir, schema=schema)


# ---------------------------------------------------------------------------
# 4. empty repo -> low-confidence defaults, flagged for confirmation
# ---------------------------------------------------------------------------
def test_empty_repo_low_confidence_defaults(tmp_path: Path, schema: dict) -> None:
    repo = tmp_path / "mystery"
    repo.mkdir()

    before = _snapshot(repo)
    proposal = discover_stack(repo)
    after = _snapshot(repo)
    assert before == after

    # Sensible defaults are proposed, but at LOW confidence.
    assert proposal.ir["ci_vendor"] == "github"
    assert proposal.ir["target_platform"] == "aws-ecs"
    assert proposal.evidence["ci_vendor"].confidence is Confidence.LOW
    assert proposal.evidence["target_platform"].confidence is Confidence.LOW
    assert proposal.lowest_confidence() is Confidence.LOW

    # The defaults are explicitly flagged for the developer to confirm (R10.2).
    joined = " ".join(proposal.assumptions).lower()
    assert "ci_vendor" in joined
    assert "target_platform" in joined
    assert "confirm" in joined

    # Even an empty repo yields a shape-valid draft (required fields present).
    assert isinstance(proposal.pipeline, Pipeline)
    jsonschema.validate(instance=proposal.ir, schema=schema)


# ---------------------------------------------------------------------------
# 5. plain Dockerfile only -> aws-ecs at LOW confidence, flagged
# ---------------------------------------------------------------------------
def test_plain_dockerfile_low_confidence(tmp_path: Path) -> None:
    repo = tmp_path / "containerish"
    repo.mkdir()
    _write(repo, "Dockerfile", "FROM alpine\n")
    _write(repo, ".github/workflows/ci.yml", "name: ci\njobs: {}\n")

    proposal = discover_stack(repo)
    target = proposal.evidence["target_platform"]
    assert target.value == "aws-ecs"
    assert target.confidence is Confidence.LOW
    assert "Dockerfile" in target.signals[0]
    assert "confirm" in target.note.lower()


# ---------------------------------------------------------------------------
# 6. security scanner config adds a security stage
# ---------------------------------------------------------------------------
def test_security_scanner_adds_stage(tmp_path: Path) -> None:
    repo = tmp_path / "secured"
    repo.mkdir()
    _write(repo, ".github/workflows/ci.yml", "name: ci\njobs: {}\n")
    _write(repo, ".snyk", "version: v1\n")

    proposal = discover_stack(repo)
    stage_ids = [s["id"] for s in proposal.ir["stages"]]
    assert "security" in stage_ids
    assert any("security" in a.lower() for a in proposal.assumptions)


# ---------------------------------------------------------------------------
# 7. render output is readable and contains evidence + confirm notes
# ---------------------------------------------------------------------------
def test_render_proposal_contains_sections(tmp_path: Path) -> None:
    repo = tmp_path / "orders-api"
    repo.mkdir()
    _write(repo, ".github/workflows/ci.yml", "name: ci\njobs: {}\n")
    _write(repo, "Dockerfile", "FROM python:3.11\n")
    _write(repo, "infra/main.tf", 'provider "aws" {}\nresource "aws_ecs_service" "s" {}\n')

    proposal = discover_stack(repo)
    text = render_proposal(proposal)

    assert "pipeline.yaml" in text
    assert "ci_vendor" in text
    assert "target_platform" in text
    assert "Evidence" in text
    assert "confirm" in text.lower()
    # The draft YAML carries the proposed values.
    assert "github" in text
    assert "aws-ecs" in text
    # Reassure that discovery did not write anything.
    assert "did not write" in text.lower() or "propose" in text.lower()


# ---------------------------------------------------------------------------
# 8. deterministic — same repo yields the same proposal IR
# ---------------------------------------------------------------------------
def test_discovery_is_deterministic(tmp_path: Path) -> None:
    repo = tmp_path / "svc"
    repo.mkdir()
    _write(repo, ".gitlab-ci.yml", "stages: []\n")
    _write(repo, "k8s/deployment.yaml", "kind: Deployment\n")
    _write(repo, "go.mod", "module svc\n")

    first = discover_stack(repo)
    second = discover_stack(repo)
    assert first.ir == second.ir
    assert isinstance(first, StackProposal)


# ---------------------------------------------------------------------------
# 9. non-directory input raises
# ---------------------------------------------------------------------------
def test_missing_repo_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(NotADirectoryError):
        discover_stack(missing)
