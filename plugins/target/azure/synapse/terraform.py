"""Terraform (HCL) the azure-synapse plugin supplies as its backing IaC.

 — the azure-synapse ``IaC`` cell is **Synapse + identity**. This module
renders that infrastructure as valid HCL using the ``hashicorp/azurerm`` provider,
parameterised by the IR and by Terraform *variables* — never by a literal
subscription id, storage account key, or secret.

What the module provisions
--------------------------------------------------------------
* **azurerm_resource_group** — the resource group everything lands in.
* **azurerm_storage_account** (ADLS Gen2, ``is_hns_enabled``) + a
 **azurerm_storage_data_lake_gen2_filesystem** — the artifact/data store the
 ``package`` verb uploads notebooks / Spark job defs / the wheel to (versioned
 blob = the promotable reference).
* **azurerm_synapse_workspace (data source)** — the EXISTING Synapse workspace is
 REFERENCED (a ``data`` source), not created: a workspace is long-lived,
 client-owned infrastructure with its own account-level setup.
* **azurerm_synapse_spark_pool** — the Spark pool the Spark job definitions run on.
* **azurerm_role_assignment** — grants the Synapse workspace's managed identity
 Storage Blob Data Contributor on the data lake so jobs can read their artifacts /
 write output without a committed key.
* **azurerm_user_assigned_identity + azurerm_federated_identity_credential (CI)** —
 a CI identity + an OIDC federated credential so the pipeline authenticates to
 Azure *keylessly* (no client secret — the R6/OIDC story), with Blob Data
 Contributor on the data lake (to upload artifacts) and Contributor on the
 workspace (to create/update + run Spark job definitions).

Why variables, not literals
----------------------------
The subscription id, tenant id, the existing Synapse workspace name/resource group,
and the CI OIDC issuer/subject differ per client and are account specifics /
secrets. They are therefore Terraform ``variable`` blocks the bootstrap step fills
from the local (git-ignored) config. The only values *derived from the IR* are the
project name (resource naming) and the region (provider + defaults), both non-secret.

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

#: Pinned Spark pool version default (reproducible, non-"latest") — mirrors jobspec.
_DEFAULT_SPARK_VERSION = "3.4"


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix.

 Azure resource names vary in their allowed charset; we normalise to
 ``[a-z0-9-]`` for a deterministic, readable prefix across the resource group,
 Spark pool, and identities. Lower-case, collapse any other run of characters to a
 single ``-``, and trim stray dashes.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def _sanitize_storage_name(raw: str) -> str:
    """Storage account names are ``[a-z0-9]`` only, 3-24 chars (no dashes).

 We strip everything non-alphanumeric, lower-case, prefix a stable token, and
 truncate to the 24-char Azure limit so the derived name is always valid.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", raw).lower()
    base = f"st{cleaned}syn"
    return base[:24] or "stappsyn"


def _sanitize_pool_name(raw: str) -> str:
    """Synapse Spark pool names are short ``[a-z0-9]`` (<= 15 chars)."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", raw).lower()
    base = f"sp{cleaned}"
    return base[:15] or "spapp"


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
        "# Inputs for the azure-synapse backing infrastructure (generated — R6: no\n"
        "# literal subscription id, tenant, storage key, or secret; the bootstrap step\n"
        "# fills these from local config).\n"
        "\n"
        'variable "location" {\n'
        '  description = "Azure region to provision into."\n'
        "  type        = string\n"
        f'  default     = "{region_default}"\n'
        "}\n"
    )
    return header + f"""
variable "project_name" {{
  description = "Project name used as the resource-name prefix."
  type        = string
}}

variable "resource_group_name" {{
  description = "Resource group the Spark pool, storage, and identities live in."
  type        = string
}}

variable "synapse_workspace_name" {{
  description = "Name of the EXISTING Synapse workspace the Spark pool + jobs belong to (referenced, not created)."
  type        = string
}}

variable "synapse_resource_group_name" {{
  description = "Resource group of the existing Synapse workspace."
  type        = string
}}

variable "spark_version" {{
  description = "Apache Spark version for the Synapse Spark pool."
  type        = string
  default     = "{_DEFAULT_SPARK_VERSION}"
}}

variable "node_size" {{
  description = "Node size for the Synapse Spark pool (Small/Medium/Large)."
  type        = string
  default     = "Small"
}}

variable "node_count" {{
  description = "Number of nodes for the Synapse Spark pool (used when autoscale is off)."
  type        = number
  default     = 3
}}

variable "artifact_filesystem" {{
  description = "ADLS Gen2 filesystem (container) name for job artifacts/output."
  type        = string
  default     = "artifacts"
}}

variable "oidc_issuer_url" {{
  description = "CI provider OIDC issuer URL (e.g. https://token.actions.githubusercontent.com)."
  type        = string
}}

variable "oidc_subject" {{
  description = "OIDC subject the CI federated credential trusts (e.g. repo:org/name:ref:refs/heads/main)."
  type        = string
}}

variable "oidc_audience" {{
  description = "Allowed OIDC audience for the Azure AD federated credential."
  type        = string
  default     = "api://AzureADTokenExchange"
}}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — Synapse ref + Spark pool + ADLS + role assignments + CI.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The region is referenced via ``var.location`` (whose default the IR drives in
 ``variables.tf``), so no region literal is hard-coded in a resource. The Synapse
 workspace is REFERENCED (a ``data`` source), never created. The data lake is read
 / written via role assignments on managed identities (no committed storage key),
 and the CI pipeline federates via its own identity + OIDC credential (keyless —
 no client secret committed, R6). There is NO container image anywhere — the
 deployable is a Synapse Spark job definition (the data-job shape).
 """
    prefix = _sanitize_name(project_name)
    storage_name = _sanitize_storage_name(project_name)
    pool_name = _sanitize_pool_name(project_name)

    return f"""\
# azure-synapse backing infrastructure (generated — design §11.9: Synapse + identity).
#
# The data-job shape (like databricks): NO container image, NO ECR, NO ECS — the
# deployable is a Synapse Spark JOB DEFINITION running an uploaded PySpark file +
# wheel from ADLS on a Synapse Spark pool.
#
# References an EXISTING Synapse workspace, provisions a Spark pool, an ADLS Gen2
# data lake for artifacts, the role assignments that let the workspace + CI access
# it keylessly, and a CI identity + OIDC federated credential.
#
# Parameterised entirely by variables (see variables.tf). No literal subscription
# id, tenant, storage key, or secret appears here (R6). Resource names derive from
# the IR project name ({project_name!r}) for a stable, readable layout (R5.3).

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
  pool_name    = "{pool_name}"
}}

# --- Resource group the pool + storage + identities land in --------------------
resource "azurerm_resource_group" "synapse" {{
  name     = var.resource_group_name
  location = var.location
}}

# --- ADLS Gen2 data lake: the package verb uploads artifacts here ---------------
# Hierarchical namespace enabled (ADLS Gen2). Jobs read artifacts / write output via
# role assignments below — no account key is ever committed (R6).
resource "azurerm_storage_account" "lake" {{
  name                     = local.storage_name
  resource_group_name      = azurerm_resource_group.synapse.name
  location                 = azurerm_resource_group.synapse.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  is_hns_enabled           = true
}}

resource "azurerm_storage_data_lake_gen2_filesystem" "artifacts" {{
  name               = var.artifact_filesystem
  storage_account_id = azurerm_storage_account.lake.id
}}

# --- Reference the EXISTING Synapse workspace (not created — client-owned) ------
data "azurerm_synapse_workspace" "ws" {{
  name                = var.synapse_workspace_name
  resource_group_name = var.synapse_resource_group_name
}}

# --- Synapse Spark pool the Spark job definitions run on ------------------------
resource "azurerm_synapse_spark_pool" "app" {{
  name                 = local.pool_name
  synapse_workspace_id = data.azurerm_synapse_workspace.ws.id
  node_size_family     = "MemoryOptimized"
  node_size            = var.node_size
  node_count           = var.node_count
  spark_version        = var.spark_version
}}

# --- Let the Synapse workspace identity read/write the data lake (no key, R6) ---
resource "azurerm_role_assignment" "ws_lake" {{
  scope                = azurerm_storage_account.lake.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_synapse_workspace.ws.identity[0].principal_id
}}

# --- CI identity + OIDC federated credential for keyless CI deploy -------------
# The CI pipeline exchanges its OIDC token for Azure access via this identity — no
# client secret is created or committed (the R6/OIDC story). It can upload artifacts
# to the data lake and create/update/run Spark job definitions in the workspace.
resource "azurerm_user_assigned_identity" "ci_deploy" {{
  name                = "${{local.name_prefix}}-ci-deploy"
  resource_group_name = azurerm_resource_group.synapse.name
  location            = azurerm_resource_group.synapse.location
}}

resource "azurerm_role_assignment" "ci_lake" {{
  scope                = azurerm_storage_account.lake.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}}

resource "azurerm_role_assignment" "ci_workspace" {{
  scope                = data.azurerm_synapse_workspace.ws.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}}

resource "azurerm_federated_identity_credential" "ci_deploy" {{
  name                = "${{local.name_prefix}}-ci-oidc"
  resource_group_name = azurerm_resource_group.synapse.name
  parent_id           = azurerm_user_assigned_identity.ci_deploy.id
  audience            = [var.oidc_audience]
  issuer              = var.oidc_issuer_url
  subject             = var.oidc_subject
}}
"""


def render_outputs_tf() -> str:
    """Render ``outputs.tf`` — the handles the pipeline/bootstrap need downstream.

 These are the non-secret identifiers the verbs reference (the Spark pool name
 the deploy/verify verbs target, the data lake account + filesystem the package
 verb uploads to, and the CI identity client id the OIDC login uses). Outputs make
 the module composable without re-deriving names elsewhere. No secret is output.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "spark_pool_name" {
  description = "Synapse Spark pool name the Spark job definitions run on."
  value       = azurerm_synapse_spark_pool.app.name
}

output "storage_account_name" {
  description = "ADLS Gen2 account the package verb uploads job artifacts to."
  value       = azurerm_storage_account.lake.name
}

output "artifact_filesystem" {
  description = "ADLS Gen2 filesystem (container) that holds job artifacts/output."
  value       = azurerm_storage_data_lake_gen2_filesystem.artifacts.name
}

output "synapse_workspace_name" {
  description = "Name of the referenced Synapse workspace the Spark jobs belong to."
  value       = data.azurerm_synapse_workspace.ws.name
}

output "ci_deploy_identity_client_id" {
  description = "User-assigned identity client id the CI pipeline logs in with via OIDC."
  value       = azurerm_user_assigned_identity.ci_deploy.client_id
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input.

 Mirrors the ``*.example`` convention: documents the keys with
 *placeholder* values only — never real subscription ids, tenant ids, or keys.
 """
    region_value = region if region else _DEFAULT_REGION
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the azure-synapse backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

location            = "{region_value}"
project_name        = "{prefix}"
resource_group_name = "{prefix}-rg"

# The EXISTING Synapse workspace the Spark pool + jobs belong to (referenced).
synapse_workspace_name      = "{prefix}-synapse"
synapse_resource_group_name = "{prefix}-synapse-rg"

# Spark pool sizing (override as needed).
spark_version = "{_DEFAULT_SPARK_VERSION}"
node_size     = "Small"
node_count    = 3

# ADLS Gen2 filesystem for job artifacts/output.
artifact_filesystem = "artifacts"

# CI OIDC trust — scope the subject to your repo/branch.
oidc_issuer_url = "https://token.actions.githubusercontent.com"
oidc_subject    = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_audience   = "{_DEFAULT_OIDC_AUDIENCE}"
"""
