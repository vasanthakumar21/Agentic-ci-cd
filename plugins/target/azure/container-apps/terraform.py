"""Terraform (HCL) the azure-container-apps plugin supplies as its IaC.

 — the azure-container-apps ``IaC`` cell is **Container Apps + identity**.
This module renders that infrastructure as valid HCL using the ``hashicorp/azurerm``
provider, parameterised by the IR and by Terraform *variables* — never by a literal
subscription id, tenant id, or secret.

What the module provisions
---------------------------------------------------------------------
* **azurerm_resource_group** — the resource group everything lands in.
* **azurerm_container_registry** — an ACR to hold the image the pipeline builds and
 pushes (the ``package`` verb target). Admin user disabled; pulls use the Container
 App's managed identity via the AcrPull role assignment below.
* **azurerm_log_analytics_workspace** — the log backend a Container Apps environment
 requires.
* **azurerm_container_app_environment** — the managed Container Apps environment the
 app runs in.
* **azurerm_user_assigned_identity (app)** — a managed identity the Container App
 uses to pull from the ACR (AcrPull) without a committed registry secret.
* **azurerm_container_app** — the Container App itself (the ``deploy`` verb updates
 its image, the ``verify`` verb polls its revision). Single-revision mode so the
 latest revision receives traffic.
* **azurerm_user_assigned_identity + azurerm_federated_identity_credential (CI)** —
 a CI identity plus an OIDC federated credential so the pipeline authenticates to
 Azure *keylessly* (no client secret — the R6/OIDC story), with an AcrPush + a
 Contributor-on-the-app role so it can push images and update the app.

Why variables, not literals
----------------------------
The subscription id, tenant id, and the CI OIDC issuer/subject differ per client and
are account specifics / secrets. Baking any of them in would make the module
non-portable and risk committing a secret. They are therefore Terraform ``variable``
blocks the bootstrap step fills from the local (git-ignored) config. The only values
*derived from the IR* are the project name (resource naming) and the region (provider
+ defaults), both non-secret.

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

#: A neutral placeholder image the Container App starts with before the first
#: deploy (never a client's real image). Overridable via the initial_image var.
_DEFAULT_INITIAL_IMAGE = "mcr.microsoft.com/k8se/quickstart:latest"


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix.

 Azure resource names vary in their allowed charset; we normalise to
 ``[a-z0-9-]`` for a deterministic, readable prefix across the resource group,
 environment, and container app. Lower-case, collapse any other run of characters
 to a single ``-``, and trim stray dashes.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def _sanitize_acr_name(raw: str) -> str:
    """ACR names are ``[a-z0-9]`` only, 5-50 chars (no dashes).

 We strip everything non-alphanumeric, lower-case, prefix a stable token, and
 truncate to the 50-char Azure limit so the derived name is always valid.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", raw).lower()
    base = f"acr{cleaned}"
    return base[:50] or "acrapp"


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
        "# Inputs for the azure-container-apps backing infrastructure (generated — R6:\n"
        "# no literal subscription id, tenant, or secret; the bootstrap step fills these\n"
        "# from local config).\n"
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
  description = "Resource group the Container App and its dependencies live in."
  type        = string
}}

variable "acr_sku" {{
  description = "SKU for the Azure Container Registry (Basic/Standard/Premium)."
  type        = string
  default     = "Standard"
}}

variable "initial_image" {{
  description = "Placeholder image the Container App starts with before the first deploy (the deploy verb overrides it)."
  type        = string
  default     = "{_DEFAULT_INITIAL_IMAGE}"
}}

variable "container_port" {{
  description = "Ingress target port the container listens on."
  type        = number
  default     = 8080
}}

variable "min_replicas" {{
  description = "Minimum number of Container App replicas."
  type        = number
  default     = 0
}}

variable "max_replicas" {{
  description = "Maximum number of Container App replicas."
  type        = number
  default     = 2
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
    """Render ``main.tf`` — Container App + environment + ACR + identity + CI federation.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The region is referenced via ``var.location`` (whose default the IR drives in
 ``variables.tf``), so no region literal is hard-coded in a resource. The Container
 App pulls from the ACR using a user-assigned managed identity (AcrPull), and the
 CI pipeline federates via its own identity + OIDC credential (keyless — no client
 secret committed, R6).
 """
    prefix = _sanitize_name(project_name)
    acr_name = _sanitize_acr_name(project_name)

    return f"""\
# azure-container-apps backing infrastructure (generated — design §11.9: Container
# Apps + identity). Provisions a Container Apps environment, the Container App, an
# ACR, a user-assigned managed identity for image pulls, and a user-assigned
# identity + OIDC federated credential for keyless CI deploys.
#
# Parameterised entirely by variables (see variables.tf). No literal subscription
# id, tenant, or secret appears here (R6). Resource names derive from the IR project
# name ({project_name!r}) for a stable, readable layout (R5.3).

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
  name_prefix = "{prefix}"
  acr_name    = "{acr_name}"
}}

# --- Resource group: everything below lands here ------------------------------
resource "azurerm_resource_group" "app" {{
  name     = var.resource_group_name
  location = var.location
}}

# --- Azure Container Registry: holds the image the pipeline builds + pushes -----
resource "azurerm_container_registry" "app" {{
  name                = local.acr_name
  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
  sku                 = var.acr_sku
  admin_enabled       = false
}}

# --- Log Analytics workspace: the Container Apps environment log backend --------
resource "azurerm_log_analytics_workspace" "app" {{
  name                = "${{local.name_prefix}}-logs"
  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
}}

# --- Container Apps environment: the managed environment the app runs in --------
resource "azurerm_container_app_environment" "app" {{
  name                       = "${{local.name_prefix}}-env"
  resource_group_name        = azurerm_resource_group.app.name
  location                   = azurerm_resource_group.app.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.app.id
}}

# --- App identity: used to pull from the ACR (no committed registry secret, R6) -
resource "azurerm_user_assigned_identity" "app" {{
  name                = "${{local.name_prefix}}-app"
  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
}}

resource "azurerm_role_assignment" "app_acr_pull" {{
  scope                = azurerm_container_registry.app.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}}

# --- The Container App the deploy verb updates and verify polls -----------------
# Single-revision mode so the latest revision receives all traffic. The image is a
# placeholder on first apply; the deploy verb sets the real image out of band so the
# committed HCL carries no client image reference. The app pulls via its identity.
resource "azurerm_container_app" "app" {{
  name                         = "${{local.name_prefix}}-app"
  resource_group_name          = azurerm_resource_group.app.name
  container_app_environment_id = azurerm_container_app_environment.app.id
  revision_mode                = "Single"

  identity {{
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }}

  registry {{
    server   = azurerm_container_registry.app.login_server
    identity = azurerm_user_assigned_identity.app.id
  }}

  ingress {{
    external_enabled = true
    target_port      = var.container_port
    traffic_weight {{
      percentage      = 100
      latest_revision = true
    }}
  }}

  template {{
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    container {{
      name   = local.name_prefix
      image  = var.initial_image
      cpu    = 0.25
      memory = "0.5Gi"
    }}
  }}

  lifecycle {{
    # The deploy verb updates the running image out of band; ignore that drift.
    ignore_changes = [template[0].container[0].image]
  }}
}}

# --- CI identity + OIDC federated credential for keyless CI deploy -------------
# The CI pipeline exchanges its OIDC token for Azure access via this identity — no
# client secret is created or committed (the R6/OIDC story). It can push images to
# the ACR and update the Container App. Issuer + subject are variables the client
# fills from their CI provider.
resource "azurerm_user_assigned_identity" "ci_deploy" {{
  name                = "${{local.name_prefix}}-ci-deploy"
  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
}}

resource "azurerm_role_assignment" "ci_acr_push" {{
  scope                = azurerm_container_registry.app.id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}}

resource "azurerm_role_assignment" "ci_app_contributor" {{
  scope                = azurerm_container_app.app.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}}

resource "azurerm_federated_identity_credential" "ci_deploy" {{
  name                = "${{local.name_prefix}}-ci-oidc"
  resource_group_name = azurerm_resource_group.app.name
  parent_id           = azurerm_user_assigned_identity.ci_deploy.id
  audience            = [var.oidc_audience]
  issuer              = var.oidc_issuer_url
  subject             = var.oidc_subject
}}
"""


def render_outputs_tf() -> str:
    """Render ``outputs.tf`` — the handles the pipeline/bootstrap need downstream.

 These are the non-secret identifiers the verbs reference (the ACR login server
 the package verb pushes to, the Container App name + resource group the deploy /
 verify verbs target, the app's ingress FQDN, and the CI identity client id the
 OIDC login uses). Outputs make the module composable without re-deriving names
 elsewhere. No secret is output.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "acr_login_server" {
  description = "ACR login server the package verb pushes images to (e.g. myacr.azurecr.io)."
  value       = azurerm_container_registry.app.login_server
}

output "acr_name" {
  description = "Azure Container Registry name."
  value       = azurerm_container_registry.app.name
}

output "container_app_name" {
  description = "Container App name the deploy/verify verbs target."
  value       = azurerm_container_app.app.name
}

output "resource_group_name" {
  description = "Resource group the Container App lives in."
  value       = azurerm_resource_group.app.name
}

output "container_app_fqdn" {
  description = "Container App ingress FQDN (used by the verify revision check)."
  value       = azurerm_container_app.app.latest_revision_fqdn
}

output "ci_deploy_identity_client_id" {
  description = "User-assigned identity client id the CI pipeline logs in with via OIDC."
  value       = azurerm_user_assigned_identity.ci_deploy.client_id
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input.

 Mirrors the ``*.example`` convention: documents the keys with
 *placeholder* values only — never real subscription ids, tenant ids, or tokens.
 """
    region_value = region if region else _DEFAULT_REGION
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the azure-container-apps backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

location            = "{region_value}"
project_name        = "{prefix}"
resource_group_name = "{prefix}-rg"

# Registry tier + the placeholder image the app starts with (deploy overrides it).
acr_sku       = "Standard"
initial_image = "{_DEFAULT_INITIAL_IMAGE}"

# Ingress + scaling.
container_port = 8080
min_replicas   = 0
max_replicas   = 2

# CI OIDC trust — scope the subject to your repo/branch.
oidc_issuer_url = "https://token.actions.githubusercontent.com"
oidc_subject    = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_audience   = "{_DEFAULT_OIDC_AUDIENCE}"
"""
