"""Terraform (HCL) the azure-functions plugin supplies as its backing IaC.

 — the azure-functions ``IaC`` cell is **Function App + identity**. This
module renders that infrastructure as valid HCL using the ``hashicorp/azurerm``
provider, parameterised by the IR and by Terraform *variables* — never by a literal
subscription id, storage connection string, or secret.

What the module provisions
--------------------------------------------------------------
* **azurerm_resource_group** — the resource group everything lands in (named from
 the IR project, region from the IR ``cloud.region`` default). Created here so the
 module is self-contained; clients with an existing group can point the name var
 at it.
* **azurerm_storage_account** — every Function App needs a backing storage account
 (the runtime stores its state / triggers there). The account name is derived
 deterministically from the project and is referenced (never a literal connection
 string — the Function App reads it by resource reference, so no secret is
 committed, R6).
* **azurerm_service_plan** — the App Service (consumption/elastic) plan the Function
 App runs on. OS type + SKU are variables so the client picks the hosting tier.
* **azurerm_linux_function_app** — the Function App itself (the ``deploy`` verb
 publishes into it, the ``verify`` verb smoke-tests it). Wired to the storage
 account + service plan by resource reference and given a *system-assigned managed
 identity* so it authenticates to other Azure resources without a committed secret.
* **azurerm_user_assigned_identity** + **azurerm_federated_identity_credential** —
 a user-assigned identity plus an OIDC federated credential so the CI pipeline
 authenticates to Azure *keylessly* (no client secret — the R6/OIDC story). The
 OIDC issuer + subject are variables the client fills from their CI provider.

Why variables, not literals
----------------------------
The subscription id, tenant id, the storage connection string, and the OIDC
issuer/subject differ per client and are account specifics / secrets. Baking any of
them in would make the module non-portable and risk committing a secret. They are
therefore Terraform ``variable`` blocks the bootstrap step fills from the local
(git-ignored) config. The only values *derived from the IR* are the project name
(resource naming) and the region (provider + defaults), both non-secret.

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
 service plan, and function app. Lower-case, collapse any other run of
 characters to a single ``-``, and trim stray dashes.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def _sanitize_storage_name(raw: str) -> str:
    """Storage account names are ``[a-z0-9]`` only, 3-24 chars (no dashes).

 We strip everything non-alphanumeric, lower-case, prefix a stable token, and
 truncate to the 24-char Azure limit so the derived name is always valid.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", raw).lower()
    base = f"st{cleaned}func"
    return base[:24] or "stappfunc"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/account-specific input (no literals).

 Keeping these as variables (not hard-coded values) is what makes the module
 portable and keeps subscription ids / OIDC subjects / tenant out of the
 committed files. The bootstrap step supplies them from the local
 git-ignored config.

 Args:
 region: the IR ``cloud.region`` — used only as the ``location`` *default*
 so the IR drives the region without hard-coding it in a resource. A
 region is not a secret, so surfacing it as a default is safe.
 """
    region_default = region if region else _DEFAULT_REGION
    header = (
        "# Inputs for the azure-functions backing infrastructure (generated — R6: no\n"
        "# literal subscription id, tenant, connection string, or secret; the bootstrap\n"
        "# step fills these from local config).\n"
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
  description = "Resource group the Function App and its dependencies live in."
  type        = string
}

variable "runtime_name" {
  description = "Function App runtime stack (e.g. python, node, dotnet-isolated)."
  type        = string
  default     = "python"
}

variable "runtime_version" {
  description = "Function App runtime version (e.g. 3.11 for python)."
  type        = string
  default     = "3.11"
}

variable "service_plan_sku" {
  description = "App Service plan SKU for the Function App (Y1 = consumption)."
  type        = string
  default     = "Y1"
}

variable "storage_account_tier" {
  description = "Tier for the backing storage account."
  type        = string
  default     = "Standard"
}

variable "storage_replication_type" {
  description = "Replication type for the backing storage account."
  type        = string
  default     = "LRS"
}

variable "oidc_issuer_url" {
  description = "CI provider OIDC issuer URL (e.g. https://token.actions.githubusercontent.com)."
  type        = string
}

variable "oidc_subject" {
  description = "OIDC subject the federated credential trusts (e.g. repo:org/name:ref:refs/heads/main)."
  type        = string
}

variable "oidc_audience" {
  description = "Allowed OIDC audience for the Azure AD federated credential."
  type        = string
  default     = "api://AzureADTokenExchange"
}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — Function App + storage + plan + identity for ``project_name``.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The region is referenced via ``var.location`` (whose default the IR drives in
 ``variables.tf``), so no region literal is hard-coded in a resource. The storage
 connection the Function App needs is passed by *resource reference*
 (``azurerm_storage_account.func.*``), never as a literal connection string, so
 the committed HCL carries no secret. The Function App gets a
 system-assigned identity and the CI pipeline authenticates via a user-assigned
 identity + OIDC federated credential (keyless — no client secret committed).
 """
    prefix = _sanitize_name(project_name)
    storage_name = _sanitize_storage_name(project_name)

    return f"""\
# azure-functions backing infrastructure (generated — design §8b: Function App +
# identity). Provisions a Function App, its backing storage account, an App Service
# plan, and a user-assigned identity with an OIDC federated credential for keyless
# CI deploys.
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

# --- Resource group: everything below lands here ------------------------------
resource "azurerm_resource_group" "func" {{
  name     = var.resource_group_name
  location = var.location
}}

# --- Storage account: the Function App runtime's required backing store --------
# The Function App reads this by resource reference — no literal connection string
# or access key is ever committed (R6).
resource "azurerm_storage_account" "func" {{
  name                     = local.storage_name
  resource_group_name      = azurerm_resource_group.func.name
  location                 = azurerm_resource_group.func.location
  account_tier             = var.storage_account_tier
  account_replication_type = var.storage_replication_type
}}

# --- App Service plan the Function App runs on (Y1 = consumption by default) ---
resource "azurerm_service_plan" "func" {{
  name                = "${{local.name_prefix}}-plan"
  resource_group_name = azurerm_resource_group.func.name
  location            = azurerm_resource_group.func.location
  os_type             = "Linux"
  sku_name            = var.service_plan_sku
}}

# --- The Function App the deploy verb publishes into and verify smoke-tests ----
# Given a system-assigned managed identity so it authenticates to other Azure
# resources without a committed secret (R6). The storage account is wired by
# resource reference (name + access key come from the resource, not a literal).
resource "azurerm_linux_function_app" "func" {{
  name                       = "${{local.name_prefix}}-func"
  resource_group_name        = azurerm_resource_group.func.name
  location                   = azurerm_resource_group.func.location
  service_plan_id            = azurerm_service_plan.func.id
  storage_account_name       = azurerm_storage_account.func.name
  storage_account_access_key = azurerm_storage_account.func.primary_access_key

  identity {{
    type = "SystemAssigned"
  }}

  site_config {{
    application_stack {{
      python_version = var.runtime_version
    }}
  }}
}}

# --- User-assigned identity + OIDC federated credential for keyless CI deploy --
# The CI pipeline exchanges its OIDC token for Azure access via this identity — no
# client secret is created or committed (the R6/OIDC story). Issuer + subject are
# variables the client fills from their CI provider.
resource "azurerm_user_assigned_identity" "ci_deploy" {{
  name                = "${{local.name_prefix}}-ci-deploy"
  resource_group_name = azurerm_resource_group.func.name
  location            = azurerm_resource_group.func.location
}}

resource "azurerm_federated_identity_credential" "ci_deploy" {{
  name                = "${{local.name_prefix}}-ci-oidc"
  resource_group_name = azurerm_resource_group.func.name
  parent_id           = azurerm_user_assigned_identity.ci_deploy.id
  audience            = [var.oidc_audience]
  issuer              = var.oidc_issuer_url
  subject             = var.oidc_subject
}}
"""


def render_outputs_tf() -> str:
    """Render ``outputs.tf`` — the handles the pipeline/bootstrap need downstream.

 These are the non-secret identifiers the verbs reference (the Function App
 name the deploy/verify verbs target, its default hostname for the smoke test,
 the resource group, and the CI identity's client id the OIDC login uses).
 Outputs make the module composable without re-deriving names elsewhere. No
 secret is output.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "function_app_name" {
  description = "Function App name the deploy/verify verbs target."
  value       = azurerm_linux_function_app.func.name
}

output "function_app_default_hostname" {
  description = "Function App default hostname (used by the verify smoke test)."
  value       = azurerm_linux_function_app.func.default_hostname
}

output "resource_group_name" {
  description = "Resource group the Function App lives in."
  value       = azurerm_resource_group.func.name
}

output "storage_account_name" {
  description = "Backing storage account name for the Function App."
  value       = azurerm_storage_account.func.name
}

output "ci_deploy_identity_client_id" {
  description = "User-assigned identity client id the CI pipeline logs in with via OIDC."
  value       = azurerm_user_assigned_identity.ci_deploy.client_id
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input.

 Mirrors the ``*.example`` convention: documents the keys with
 *placeholder* values only — never real subscription ids, connection strings, or
 tokens.
 """
    region_value = region if region else _DEFAULT_REGION
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the azure-functions backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

location            = "{region_value}"
project_name        = "{prefix}"
resource_group_name = "{prefix}-rg"

# Function App runtime (override for node/dotnet-isolated/etc.).
runtime_name    = "python"
runtime_version = "3.11"

# Hosting + storage tiers (override as needed).
service_plan_sku         = "Y1"
storage_account_tier     = "Standard"
storage_replication_type = "LRS"

# CI OIDC trust — scope the subject to your repo/branch.
oidc_issuer_url = "https://token.actions.githubusercontent.com"
oidc_subject    = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_audience   = "{_DEFAULT_OIDC_AUDIENCE}"
"""
