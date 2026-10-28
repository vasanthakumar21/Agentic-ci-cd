"""Tests for the instruction-driven agent (task 10.3, R5.1/R5.2/R10.1/R10.2).

The agent is the *driver* that turns a free-text instruction like::

    generate for ci_vendor=github target_platform=aws-ecs with region=eu-west-2 project=orders-api

into generation — by assembling the IR (discovery draft + explicit overrides,
explicit winning) and routing it THROUGH ``cicdgen.core.generate`` (the plugins).

Everything runs through an *injected* in-memory plugin registry (the same fake CI
+ target plugins style as ``test_core_generate.py``) so generation works without
the real plugins and without the on-disk plugin tree.

Proves:
    * the parser extracts the two axes + overrides (quoted values, any ordering);
    * the assembled IR carries the explicit axes + overrides (explicit > discovery);
    * the driver dry-runs by default → a plan, with NOTHING written to disk;
    * ``apply=True`` writes via the core (through the registry/plugins) into a tmp
      repo — the agent never writes pipeline files itself;
    * a required axis missing from BOTH instruction and discovery returns a
      needs-confirmation result (no guessing — R10.2);
    * discovery seeds gap-fills (language) while the instruction still wins the axes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cicdgen.agent import DriverResult, parse_instruction, run_instruction
from cicdgen.core import MANIFEST_RELPATH, GenerationPlan, GenerationResult, load_manifest
from cicdgen.plugins import (
    Capability,
    CIVendorPlugin,
    GeneratedFile,
    IaCArtifacts,
    PluginFamily,
    StageGraph,
    TargetPlatformPlugin,
    VerbContext,
    VerbResult,
)

#: Full-capability stand-in set so the §11.2 gate (task 17) passes for the IRs the
#: agent driver builds in these tests.
_ALL_CAPS = frozenset(Capability)


# ---------------------------------------------------------------------------
# Fake plugins (in-memory registry) — mirror test_core_generate.py
# ---------------------------------------------------------------------------
class _FakeCI(CIVendorPlugin):
    name = "github"

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        stage_ids = ",".join(s.id for s in graph.stages)
        return (
            GeneratedFile(
                path=".github/workflows/ci.yml",
                content=f"# ci\nstages: {stage_ids}\n",
            ),
        )


class _FakeTarget(TargetPlatformPlugin):
    name = "aws-ecs"
    capabilities = _ALL_CAPS

    def _verb(self, ctx: VerbContext) -> VerbResult:
        return VerbResult(
            commands=(ctx.verb.value,),
            files=(GeneratedFile(path=f"verb/{ctx.verb.value}.sh", content=ctx.verb.value + "\n"),),
        )

    lint = _verb
    test = _verb
    build = _verb
    package = _verb
    deploy = _verb
    verify = _verb

    def supply_iac(self, pipeline) -> IaCArtifacts:  # noqa: ANN001 - test helper
        return IaCArtifacts(
            files=(GeneratedFile(path="iac/main.tf", content="# terraform\n"),),
            summary="demo infra",
        )


def _registry() -> dict:
    return {
        (PluginFamily.CI_VENDOR, "github"): _FakeCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): _FakeTarget(),
    }


# ---------------------------------------------------------------------------
# Fake client repos (filesystem signals discovery keys off)
# ---------------------------------------------------------------------------
def _write(root: Path, rel: str, content: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _github_awsecs_repo(tmp_path: Path) -> Path:
    """A repo whose signals discover github x aws-ecs x aws (HIGH confidence)."""
    repo = tmp_path / "orders-api"
    repo.mkdir()
    _write(repo, ".github/workflows/ci.yml", "name: ci\njobs: {}\n")
    _write(repo, "Dockerfile", "FROM python:3.11\n")
    _write(repo, "infra/main.tf", 'provider "aws" {}\nresource "aws_ecs_service" "s" {}\n')
    _write(repo, "pyproject.toml", "[project]\nname='orders-api'\n")
    return repo


def _empty_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "blank"
    repo.mkdir()
    return repo


_INSTRUCTION = (
    "generate for ci_vendor=github target_platform=aws-ecs "
    "with region=eu-west-2 project=orders-api"
)


# ===========================================================================
# Parser
# ===========================================================================
def test_parser_extracts_axes_and_overrides() -> None:
    parsed = parse_instruction(_INSTRUCTION)
    assert parsed.ci_vendor == "github"
    assert parsed.target_platform == "aws-ecs"
    assert parsed.overrides == {"region": "eu-west-2", "project": "orders-api"}


def test_parser_is_order_and_phrasing_insensitive() -> None:
    parsed = parse_instruction(
        "target_platform=databricks please ci_vendor=azure-devops project=\"my app\""
    )
    assert parsed.ci_vendor == "azure-devops"
    assert parsed.target_platform == "databricks"
    # quoted value with a space is preserved
    assert parsed.overrides["project"] == "my app"


def test_parser_missing_axis_is_none_not_guessed() -> None:
    parsed = parse_instruction("generate for target_platform=aws-ecs with region=eu-west-2")
    assert parsed.ci_vendor is None
    assert parsed.target_platform == "aws-ecs"
    assert parsed.missing_axes() == ("ci_vendor",)


def test_parser_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_instruction("   ")


# ===========================================================================
# IR assembly — explicit instruction wins over discovery
# ===========================================================================
def test_assembled_ir_carries_axes_and_overrides(tmp_path: Path) -> None:
    repo = _github_awsecs_repo(tmp_path)
    result = run_instruction(_INSTRUCTION, repo, registry=_registry())

    assert not result.needs_confirmation
    assert result.ci_vendor == "github"
    assert result.target_platform == "aws-ecs"
    assert result.assembled_ir["project"]["name"] == "orders-api"
    # region placed under cloud (aws provider discovered) OR forwarded as a setting
    cloud = result.assembled_ir.get("cloud", {})
    assert cloud.get("region") == "eu-west-2"


def test_explicit_instruction_overrides_discovery(tmp_path: Path) -> None:
    # Repo discovers github x aws-ecs, but the instruction forces azure-devops.
    repo = _github_awsecs_repo(tmp_path)
    reg = {
        (PluginFamily.CI_VENDOR, "azure-devops"): _AzdoCI(),
        (PluginFamily.TARGET_PLATFORM, "aws-ecs"): _FakeTarget(),
    }
    result = run_instruction(
        "generate for ci_vendor=azure-devops target_platform=aws-ecs with project=orders-api",
        repo,
        registry=reg,
    )
    assert result.ci_vendor == "azure-devops"  # instruction beat discovery's 'github'
    assert any("overrides discovery" in g for g in result.gap_fills)


class _AzdoCI(CIVendorPlugin):
    name = "azure-devops"

    def emit_pipeline_files(self, graph: StageGraph) -> tuple[GeneratedFile, ...]:
        return (GeneratedFile(path="azure-pipelines.yml", content="pool: default\n"),)


def test_discovery_gap_fills_language(tmp_path: Path) -> None:
    repo = _github_awsecs_repo(tmp_path)
    result = run_instruction(_INSTRUCTION, repo, registry=_registry())
    # pyproject.toml -> python, gap-filled from discovery into the IR
    assert result.assembled_ir["project"]["language"] == "python"


# ===========================================================================
# Dry-run by default — a plan, nothing written (R10 safety posture)
# ===========================================================================
def test_dry_run_by_default_returns_plan_and_writes_nothing(tmp_path: Path) -> None:
    repo = _github_awsecs_repo(tmp_path)
    before = set(repo.rglob("*"))

    result = run_instruction(_INSTRUCTION, repo, registry=_registry())

    assert result.applied is False
    assert isinstance(result.plan, GenerationPlan)
    assert result.result is None
    assert result.plan.files, "the plan should list files that WOULD be written"

    # the agent/driver wrote nothing new — no manifest, no generated files
    after = set(repo.rglob("*"))
    assert after == before
    assert not (repo / MANIFEST_RELPATH).exists()
    assert not (repo / ".github/workflows/ci.yml").read_text(encoding="utf-8").startswith("# ci")


# ===========================================================================
# apply=True writes THROUGH the core/plugins (not by the agent directly)
# ===========================================================================
def test_apply_writes_through_core_and_plugins(tmp_path: Path) -> None:
    repo = _github_awsecs_repo(tmp_path)
    result = run_instruction(_INSTRUCTION, repo, registry=_registry(), apply=True)

    assert result.applied is True
    assert isinstance(result.result, GenerationResult)

    # Files produced by the (fake) plugins exist — proof generation went THROUGH them.
    ci = (repo / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert ci.startswith("# ci\nstages: ")  # the _FakeCI content, not the client's original
    assert (repo / "verb/build.sh").is_file()      # target verb support file
    assert (repo / "verb/deploy.sh").is_file()
    assert (repo / "iac/main.tf").is_file()         # target IaC

    # The core wrote + recorded the manifest (the generator's memory).
    manifest = load_manifest(repo)
    assert manifest is not None
    assert manifest.ci_vendor == "github"
    assert manifest.target_platform == "aws-ecs"
    assert ".github/workflows/ci.yml" in manifest.paths
    assert "iac/main.tf" in manifest.paths


def test_agent_does_not_author_files_itself(tmp_path: Path) -> None:
    """Every written file (except the manifest) must originate from a plugin.

    The driver assembles IR inputs only; it must not invent files. We assert the
    on-disk generated set equals exactly the plugin-produced set the core planned.
    """
    repo = _empty_repo(tmp_path)
    # instruction supplies both axes so no discovery signal is needed
    instr = "generate for ci_vendor=github target_platform=aws-ecs with project=blank"
    result = run_instruction(instr, repo, registry=_registry(), apply=True)
    assert result.result is not None

    planned = set(result.result.manifest.paths)
    on_disk = {
        p.relative_to(repo).as_posix()
        for p in repo.rglob("*")
        if p.is_file() and p.relative_to(repo).as_posix() != MANIFEST_RELPATH
    }
    assert on_disk == planned, "agent wrote files the plugins/manifest do not own"


# ===========================================================================
# Needs-confirmation — missing axis with no discovery signal (R10.2)
# ===========================================================================
def test_missing_axis_no_signal_needs_confirmation(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path)  # no CI signal, no deploy signal
    # Only target named; ci_vendor unstated and the empty repo has no confident signal.
    result = run_instruction(
        "generate for target_platform=aws-ecs with project=blank",
        repo,
        registry=_registry(),
    )
    assert isinstance(result, DriverResult)
    assert result.needs_confirmation is True
    assert result.plan is None and result.result is None
    assert any("ci_vendor" in q for q in result.questions)
    # nothing written
    assert not (repo / MANIFEST_RELPATH).exists()


def test_confident_discovery_fills_missing_axis(tmp_path: Path) -> None:
    """When the instruction omits an axis but discovery is confident, fill it."""
    repo = _github_awsecs_repo(tmp_path)  # HIGH-confidence github x aws-ecs
    # Omit ci_vendor from the instruction; discovery should supply github confidently.
    result = run_instruction(
        "generate for target_platform=aws-ecs with project=orders-api",
        repo,
        registry=_registry(),
    )
    assert result.needs_confirmation is False
    assert result.ci_vendor == "github"
    assert any("gap-filled from discovery" in g for g in result.gap_fills)


# ===========================================================================
# use_discovery=False — build purely from the instruction
# ===========================================================================
def test_no_discovery_builds_from_instruction_only(tmp_path: Path) -> None:
    repo = _empty_repo(tmp_path)
    result = run_instruction(
        _INSTRUCTION,
        repo,
        registry=_registry(),
        use_discovery=False,
    )
    assert result.needs_confirmation is False
    assert result.ci_vendor == "github"
    assert result.target_platform == "aws-ecs"
    assert result.assembled_ir["project"]["name"] == "orders-api"
    # region has no known provider (no discovery) -> forwarded as a setting, not invented
    assert "cloud" not in result.assembled_ir
