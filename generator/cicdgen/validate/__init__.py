"""Validation helpers for generated artifacts.

Lightweight, dependency-light validators for the pipeline files the CI-vendor
plugins emit, used by the sample-generation tests to prove the committed
artifacts are both *parseable* YAML and *structurally* valid for their vendor —
without requiring a real account/run:

 * ``github_workflow`` — GitHub Actions workflow (``.github/workflows/ci.yml``),
 backing ``tests/test_github_sample_generation.py``.
 * ``azure_pipeline`` — Azure Pipelines (``azure-pipelines.yml``), encoding the
 Azure DevOps column, backing
 ``tests/test_azdo_sample_generation.py``.
 * ``terraform_hcl`` — structural HCL validation for the target-platform IaC
 (the local stand-in for ``terraform plan`` where the binary/account are
 unavailable — R9), backing ``tests/test_aws_ecs_terraform.py``.
"""

from __future__ import annotations

from cicdgen.validate.azure_pipeline import (
    AzurePipelineValidationError,
    assert_mapping_table_rows_represented,
    validate_azure_pipeline_structure,
)
from cicdgen.validate.github_workflow import (
    GitHubWorkflowValidationError,
    fetch_github_workflow_schema,
    validate_against_schema,
    validate_workflow_structure,
)
from cicdgen.validate.target_mapping import (
    IAC_ROW,
    MAPPED_VERBS,
    TARGET_MAPPING,
    MappingReport,
    TargetColumn,
    TargetMappingValidationError,
    VerbExpectation,
    assert_cross_platform_contrast,
    assert_full_table_coverage,
    assert_target_matches_mapping,
    expected_rows,
)
from cicdgen.validate.terraform_hcl import (
    EXPECTED_OUTPUTS,
    EXPECTED_RESOURCES,
    EXPECTED_VARIABLES,
    HclSummary,
    TerraformHclValidationError,
    assert_expected_resources,
    assert_no_account_specifics,
    validate_hcl_structure,
    validate_terraform_module,
)

__all__ = [
    # GitHub Actions
    "GitHubWorkflowValidationError",
    "validate_workflow_structure",
    "validate_against_schema",
    "fetch_github_workflow_schema",
    # Azure Pipelines
    "AzurePipelineValidationError",
    "validate_azure_pipeline_structure",
    "assert_mapping_table_rows_represented",
    # Terraform HCL
    "TerraformHclValidationError",
    "HclSummary",
    "validate_hcl_structure",
    "validate_terraform_module",
    "assert_expected_resources",
    "assert_no_account_specifics",
    "EXPECTED_RESOURCES",
    "EXPECTED_VARIABLES",
    "EXPECTED_OUTPUTS",
    # Target mapping table
    "TargetMappingValidationError",
    "assert_target_matches_mapping",
    "assert_full_table_coverage",
    "assert_cross_platform_contrast",
    "expected_rows",
    "MappingReport",
    "TargetColumn",
    "VerbExpectation",
    "TARGET_MAPPING",
    "MAPPED_VERBS",
    "IAC_ROW",
]
