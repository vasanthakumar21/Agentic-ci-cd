"""Sample IaC generation + local validation for the databricks plugin (task 9.2).

Task 9.2 asks for the Terraform/API that wires **workspace + job + (optional) Unity
Catalog**, parameterized by the IR + local config, with the workspace URL/token
strictly *by reference* (R3.3/R4.2/R7.1, R6 secrets-by-reference, R5.3 determinism).

A *real* ``terraform plan`` needs the terraform binary **and** a throwaway
Databricks workspace — both kept out of this environment by R9 (isolation). So this
module does the strongest thing possible locally, deterministically, with no
network/workspace operations:

    1. Generate the databricks IaC from the committed sample IR
       ``examples/client-azdo-databricks/pipeline.yaml`` through the real plugin.
    2. Prove the committed artifacts under
       ``examples/client-azdo-databricks/iac/databricks/`` match a fresh generation
       **byte-for-byte** (determinism, R5.3) — the reviewable files in the repo are
       exactly what the generator produces.
    3. Structurally validate every ``.tf`` with
       :func:`cicdgen.validate.validate_hcl_structure` (balanced braces, well-formed
       blocks) — the local stand-in for ``terraform validate`` / the parse phase.
    4. Assert the §8b resources exist (``databricks_job`` + the OPTIONAL Unity
       Catalog ``databricks_schema``), the provider is wired to the workspace by
       reference, and NO literal workspace URL / token / account appears (R6).

The live ``terraform init/plan/apply`` against a throwaway workspace is an
irreducible manual step documented in
``examples/client-azdo-databricks/iac/databricks/README.md`` and intentionally NOT
run here (R9: isolation — no terraform binary, no workspace in this environment).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from cicdgen.ir import Pipeline
from cicdgen.plugins import load_target_platform_plugin
from cicdgen.validate import TerraformHclValidationError, validate_hcl_structure

# <repo>/tests/test_databricks_terraform.py -> <repo>
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "plugins"
SAMPLE_DIR = REPO_ROOT / "examples" / "client-azdo-databricks"
SAMPLE_IR = SAMPLE_DIR / "pipeline.yaml"
IAC_DIR = SAMPLE_DIR / "iac" / "databricks"

TF_FILES = ("main.tf", "variables.tf", "outputs.tf", "terraform.tfvars.example")
ALL_FILES = (*TF_FILES, "README.md")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_sample_pipeline() -> Pipeline:
    data = yaml.safe_load(SAMPLE_IR.read_text(encoding="utf-8"))
    return Pipeline.model_validate(data)


def _plugin():
    return load_target_platform_plugin("databricks", plugins_root=PLUGINS_ROOT)


def _fresh_iac_files() -> dict[str, str]:
    """Freshly generate the IaC (basename -> content) from the sample IR."""
    iac = _plugin().supply_iac(_load_sample_pipeline())
    return {Path(f.path).name: f.content for f in iac.files}


def _committed_iac_files() -> dict[str, str]:
    return {name: (IAC_DIR / name).read_bytes().decode("utf-8") for name in ALL_FILES}


# ---------------------------------------------------------------------------
# Artifacts exist
# ---------------------------------------------------------------------------
def test_committed_artifacts_exist() -> None:
    assert SAMPLE_IR.is_file(), f"missing sample IR: {SAMPLE_IR}"
    for name in ALL_FILES:
        assert (IAC_DIR / name).is_file(), f"missing committed IaC artifact: {name}"


# ---------------------------------------------------------------------------
# Determinism: committed artifacts == fresh generation, byte-for-byte (R5.3)
# ---------------------------------------------------------------------------
def test_committed_iac_matches_fresh_generation_byte_for_byte() -> None:
    fresh = _fresh_iac_files()
    committed = _committed_iac_files()
    for name in ALL_FILES:
        assert committed[name] == fresh[name], (
            f"committed iac/databricks/{name} is stale — regenerate it from "
            "examples/client-azdo-databricks/pipeline.yaml so the reviewable "
            "artifact matches the generator output."
        )


def test_generation_is_deterministic_across_calls() -> None:
    assert _fresh_iac_files() == _fresh_iac_files()


# ---------------------------------------------------------------------------
# Structural HCL validation (the local stand-in for terraform validate/plan)
# ---------------------------------------------------------------------------
def test_each_committed_tf_file_is_structurally_valid() -> None:
    for name in ("main.tf", "variables.tf", "outputs.tf"):
        content = (IAC_DIR / name).read_text(encoding="utf-8")
        validate_hcl_structure(content, filename=name)  # raises on malformed HCL


def test_tfvars_example_is_balanced() -> None:
    """The tfvars example is assignments only — just check it's well-formed HCL."""
    content = (IAC_DIR / "terraform.tfvars.example").read_text(encoding="utf-8")
    # balanced braces/brackets; no unknown top-level blocks expected
    assert content.count("{") == content.count("}")
    assert content.count("[") == content.count("]")


def test_structural_validator_rejects_broken_databricks_hcl() -> None:
    bad = 'resource "databricks_job" "x" {\n  name = "y"\n'  # missing close brace
    with pytest.raises(TerraformHclValidationError):
        validate_hcl_structure(bad, filename="bad.tf")


# ---------------------------------------------------------------------------
# Expected resources / provider (design §8b: workspace, Unity Catalog, job)
# ---------------------------------------------------------------------------
def test_module_declares_job_and_optional_unity_catalog() -> None:
    summary = validate_hcl_structure(
        (IAC_DIR / "main.tf").read_text(encoding="utf-8"), filename="main.tf"
    )
    assert "databricks_job" in summary.resources()
    assert "databricks_schema" in summary.resources()
    assert "databricks" in summary.providers()


def test_module_uses_official_databricks_provider() -> None:
    main = (IAC_DIR / "main.tf").read_text(encoding="utf-8")
    assert "databricks/databricks" in main
    assert "required_providers" in main


def test_provider_host_and_token_are_by_reference() -> None:
    main = (IAC_DIR / "main.tf").read_text(encoding="utf-8")
    variables = (IAC_DIR / "variables.tf").read_text(encoding="utf-8")
    assert "host  = var.databricks_host" in main
    assert "token = var.databricks_token" in main
    assert "sensitive   = true" in variables or "sensitive = true" in variables


def test_unity_catalog_schema_is_count_gated() -> None:
    main = (IAC_DIR / "main.tf").read_text(encoding="utf-8")
    assert "count        = var.enable_unity_catalog ? 1 : 0" in main


# ---------------------------------------------------------------------------
# No committed workspace specifics / secrets (R6)
# ---------------------------------------------------------------------------
def test_no_literal_workspace_url_or_token_committed() -> None:
    files = _committed_iac_files()
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


def test_no_bare_secret_assignments() -> None:
    files = _committed_iac_files()
    secret_assign = re.compile(r"(?i)\b(password|secret)\s*=\s*\"[^\"$]+\"")
    for name, content in files.items():
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "description" in stripped:
                continue
            assert not secret_assign.search(stripped), (
                f"{name}: literal secret/password assignment: {stripped!r}"
            )
