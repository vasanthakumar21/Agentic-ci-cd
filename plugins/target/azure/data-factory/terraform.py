"""Terraform (HCL) the azure-data-factory plugin supplies as its IaC.

 — the azure-data-factory ``IaC`` cell is **ADF + identity**. This module
renders that infrastructure as valid HCL using the ``hashicorp/azurerm`` provider,
parameterised by the IR and by Terraform *variables* — never by a literal
subscription id, storage connection string, or secret.

What the module provisions
-------------------------------------------------------------------
* **azurerm_resource_group** — the resource group everything lands in.
* **azurerm_storage_account** (ADLS Gen2) + a
 **azurerm_storage_data_lake_gen2_filesystem** — the artifact/definition store the
 ``package`` verb uploads the committed ADF JSON definitions to (versioned blob =
 the promotable reference).
* **azurerm_data_factory** — the Data Factory itself (the ``deploy`` verb upserts a
 pipeline into it; the ``verify`` verb triggers + polls a run). Given a
 system-assigned managed identity so linked services authenticate to storage
 without a committed key.
* **azurerm_data_factory_pipeline** — a starter pipeline resource so the factory is
 usable immediately; the ``deploy`` verb maintains the real pipeline out of band.
* **azurerm_role_assignment** — grants the Data Factory's managed identity Storage
 Blob Data Contributor on the data lake so activities read/write without a key.
* **azurerm_user_assigned_identity + azurerm_federated_identity_credential (CI)** —
 a CI identity + an OIDC federated credential so the pipeline authenticates to
 Azure *keylessly* (no client secret — the R6/OIDC story), with Blob Data
 Contributor on the data lake and Contributor on the factory (to upsert + run
 pipelines).

Why variables, not literals
----------------------------
The subscription id, tenant id, and CI OIDC issuer/subject differ per client and are
account specifics / secrets. They are therefore Terraform ``variable`` blocks the
bootstrap step fills from the local (git-ignored) config. The only values *derived
from the IR* are the project name (resource naming) and the region (provider +
defaults), both non-secret.

Determinism
------------------
HCL is assembled from fixed, ordered string templates — no timestamps, no random
suffixes, stable block order — so regenerating from the same IR is byte-identical.
"""

from __future__ import annotations

import re

#: Default Azure region used only when the IR carries no ``cloud.region`` — a
#: documented, non-secret constant (overridable via the ``location`` variable).
_DEFAULT_REGION = "uksouth"

#: Default OIDC audience for an Azure AD federated credential (non-secret constant).
_DEFAULT_OIDC_AUDIENCE = "api://AzureADTokenExchange"


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix.

 Azure resource names vary in their allowed charset; we normalise to
 ``[a-z0-9-]`` for a deterministic, readable prefix across the resource group,
 data factory, and identities. Lower-case, collapse any other run of characters to
 a single ``-``, and trim stray dashes.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def _sanitize_storage_name(raw: str) -> str:
    """Storage account names are ``[a-z0-9]`` only, 3-24 chars (no dashes).

 We strip everything non-alphanumeric, lower-case, prefix a stable token, and
 truncate to the 24-char Azure limit so the derived name is always valid.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", raw).lower()
    base = f"st{cleaned}adf"
    return base[:24] or "stappadf"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/account-specific input (no literals).

 Keeping these as variables (not hard-coded values) is what makes the module
 portable and keeps subscription ids / OIDC subjects / tenant out of the committed
 files. The bootstrap step supplies them from the local git-ignored config.

 Args:
 region: the IR ``cloud.region`` — used only as the ``location`` *default* so
 the IR drives the region without hard-coding it in a resource.
 """
    region_default = region if region else _DEFAULT_REGION
    header = (
        "# Inputs for the azure-data-factory backing infrastructure (generated — R6:\n"
        "# no literal subscription id, tenant, connection string, or secret; the\n"
        "# bootstrap step fills these from local config).\n"
        "\n"
        'variable "location" {\n'
        '  description = "Azure region to provision into."\n'
        "  type        = string\n"
        f'  default     = "{region_default}"\n'
        "}\n"
    )
    return header + """
variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group the Data Factory and its dependencies live in."
  type        = string
}

variable "artifact_filesystem" {
  description = "ADLS Gen2 filesystem (container) name for ADF definitions/artifacts."
  type        = string
  default     = "definitions"
}

variable "oidc_issuer_url" {
  description = "CI provider OIDC issuer URL (e.g. https://token.actions.githubusercontent.com)."
  type        = string
}

variable "oidc_subject" {
  description = "OIDC subject the CI federated credential trusts (e.g. repo:org/name:ref:refs/heads/main)."
  type        = string
}

variable "oidc_audience" {
  description = "Allowed OIDC audience for the Azure AD federated credential."
  type        = string
  default     = "api://AzureADTokenExchange"
}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — Data Factory + pipeline + ADLS + roles + CI federation.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The region is referenced via ``var.location`` (whose default the IR drives in
 ``variables.tf``), so no region literal is hard-coded in a resource. The Data
 Factory gets a system-assigned managed identity so linked services authenticate
 to storage without a committed key; the CI pipeline federates via its own
 identity + OIDC credential (keyless — no client secret committed, R6). There is
 NO container image anywhere — the deployable is an ADF pipeline (the data-job
 shape).
 """
    prefix = _sanitize_name(project_name)
    storage_name = _sanitize_storage_name(project_name)

    return f"""\
# azure-data-factory backing infrastructure (generated — design §11.9: ADF + identity).
#
# The data-job shape (like databricks): NO container image, NO ACR, NO ECS — the
# deployable is an Azure Data Factory PIPELINE (ARM/JSON definitions) the deploy verb
# upserts and the verify verb triggers + polls.
#
# Parameterised entirely by variables (see variables.tf). No literal subscription
# id, tenant, connection string, or secret appears here (R6). Resource names derive
# from the IR project name ({project_name!r}) for a stable, readable layout (R5.3).

terraform {{
  required_version = ">= 1.5.0"
  required_providers {{
    azurerm = {{
      source  = "hashicorp/azurerm"
      version = ">= 3.80"
    }}
  }}
}}

provider "azurerm" {{
  features {{}}
}}

locals {{
  name_prefix  = "{prefix}"
  storage_name = "{storage_name}"
}}

# --- Resource group the factory + storage + identities land in -----------------
resource "azurerm_resource_group" "adf" {{
  name     = var.resource_group_name
  location = var.location
}}

# --- ADLS Gen2 data lake: the package verb uploads ADF definitions/artifacts ----
# Activities read artifacts / write output via the role assignment below — no
# account key is ever committed (R6).
resource "azurerm_storage_account" "lake" {{
  name                     = local.storage_name
  resource_group_name      = azurerm_resource_group.adf.name
  location                 = azurerm_resource_group.adf.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  is_hns_enabled           = true
}}

resource "azurerm_storage_data_lake_gen2_filesystem" "definitions" {{
  name               = var.artifact_filesystem
  storage_account_id = azurerm_storage_account.lake.id
}}

# --- The Data Factory the deploy verb upserts pipelines into --------------------
# A system-assigned managed identity lets linked services authenticate to storage
# without a committed connection string / key (R6).
resource "azurerm_data_factory" "app" {{
  name                = "${{local.name_prefix}}-adf"
  resource_group_name = azurerm_resource_group.adf.name
  location            = azurerm_resource_group.adf.location

  identity {{
    type = "SystemAssigned"
  }}
}}

# --- Starter pipeline so the factory is usable immediately ----------------------
# The deploy verb maintains the real pipeline out of band (from the committed JSON);
# this resource just ensures a pipeline of the expected name exists.
resource "azurerm_data_factory_pipeline" "app" {{
  name            = "${{local.name_prefix}}-pipeline"
  data_factory_id = azurerm_data_factory.app.id
}}

# --- Let the factory identity read/write the data lake (no key, R6) ------------
resource "azurerm_role_assignment" "adf_lake" {{
  scope                = azurerm_storage_account.lake.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_data_factory.app.identity[0].principal_id
}}

# --- CI identity + OIDC federated credential for keyless CI deploy -------------
# The CI pipeline exchanges its OIDC token for Azure access via this identity — no
# client secret is created or committed (the R6/OIDC story). It can upload
# definitions to the data lake and upsert/run ADF pipelines.
resource "azurerm_user_assigned_identity" "ci_deploy" {{
  name                = "${{local.name_prefix}}-ci-deploy"
  resource_group_name = azurerm_resource_group.adf.name
  location            = azurerm_resource_group.adf.location
}}

resource "azurerm_role_assignment" "ci_lake" {{
  scope                = azurerm_storage_account.lake.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}}

resource "azurerm_role_assignment" "ci_factory" {{
  scope                = azurerm_data_factory.app.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}}

resource "azurerm_federated_identity_credential" "ci_deploy" {{
  name                = "${{local.name_prefix}}-ci-oidc"
  resource_group_name = azurerm_resource_group.adf.name
  parent_id           = azurerm_user_assigned_identity.ci_deploy.id
  audience            = [var.oidc_audience]
  issuer              = var.oidc_issuer_url
  subject             = var.oidc_subject
}}
"""


def render_outputs_tf() -> str:
    """Render ``outputs.tf`` — the handles the pipeline/bootstrap need downstream.

 These are the non-secret identifiers the verbs reference (the Data Factory name +
 resource group the deploy/verify verbs target, the data lake account the package
 verb uploads to, and the CI identity client id the OIDC login uses). Outputs make
 the module composable without re-deriving names elsewhere. No secret is output.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "data_factory_name" {
  description = "Data Factory name the deploy/verify verbs target."
  value       = azurerm_data_factory.app.name
}

output "resource_group_name" {
  description = "Resource group the Data Factory lives in."
  value       = azurerm_resource_group.adf.name
}

output "storage_account_name" {
  description = "ADLS Gen2 account the package verb uploads ADF definitions to."
  value       = azurerm_storage_account.lake.name
}

output "ci_deploy_identity_client_id" {
  description = "User-assigned identity client id the CI pipeline logs in with via OIDC."
  value       = azurerm_user_assigned_identity.ci_deploy.client_id
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input.

 Mirrors the ``*.example`` convention: documents the keys with
 *placeholder* values only — never real subscription ids, tenant ids, or
 connection strings.
 """
    region_value = region if region else _DEFAULT_REGION
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the azure-data-factory backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

location            = "{region_value}"
project_name        = "{prefix}"
resource_group_name = "{prefix}-rg"

# ADLS Gen2 filesystem for ADF definitions/artifacts.
artifact_filesystem = "definitions"

# CI OIDC trust — scope the subject to your repo/branch.
oidc_issuer_url = "https://token.actions.githubusercontent.com"
oidc_subject    = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_audience   = "{_DEFAULT_OIDC_AUDIENCE}"
"""
