"""Terraform (HCL) the azure-aks plugin supplies as its backing IaC.

 — the azure-aks ``IaC`` cell is **AKS refs + workload identity**. This
module renders that infrastructure as valid HCL using the ``hashicorp/azurerm``
provider, parameterised by the IR and by Terraform *variables* — never by a literal
subscription id, tenant id, or secret.

What the module provisions
----------------------------------------------------------
* **azurerm_container_registry** — an ACR to hold the image the pipeline builds and
 pushes (the ``package`` verb target). Admin user stays disabled; pulls rely on the
 AKS→ACR role assignment below, not on a committed credential.
* **azurerm_kubernetes_cluster (data source)** — the EXISTING AKS cluster is
 REFERENCED (a ``data`` source), not created: clusters are long-lived, client-owned
 infrastructure. Its OIDC issuer URL drives the workload-identity federated
 credential below.
* **azurerm_role_assignment (AcrPull)** — lets the cluster's kubelet identity pull
 from the ACR, so image pulls need no committed registry secret.
* **azurerm_user_assigned_identity** — the workload identity the pod federates to
 (annotated on the ServiceAccount) so the app authenticates to Azure resources
 without a committed secret.
* **azurerm_federated_identity_credential (workload)** — binds the user-assigned
 identity to the Kubernetes ServiceAccount through the AKS cluster's OIDC issuer —
 this is the AKS Workload Identity binding.
* **azurerm_user_assigned_identity + azurerm_federated_identity_credential (CI)** —
 a separate identity + an OIDC federated credential so the CI pipeline
 authenticates to Azure *keylessly* (no client secret — the R6/OIDC story). The CI
 OIDC issuer + subject are variables the client fills from their CI provider.

Why variables, not literals
----------------------------
The subscription id, tenant id, the existing AKS cluster name/resource group, and
the CI OIDC issuer/subject differ per client and are account specifics / secrets.
Baking any of them in would make the module non-portable and risk committing a
secret. They are therefore Terraform ``variable`` blocks the bootstrap step fills
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


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix.

 Azure resource names vary in their allowed charset; we normalise to
 ``[a-z0-9-]`` for a deterministic, readable prefix across the identities and
 role assignments. Lower-case, collapse any other run of characters to a single
 ``-``, and trim stray dashes.
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
 the IR drives the region without hard-coding it in a resource. A region
 is not a secret, so surfacing it as a default is safe.
 """
    region_default = region if region else _DEFAULT_REGION
    header = (
        "# Inputs for the azure-aks backing infrastructure (generated — R6: no literal\n"
        "# subscription id, tenant, or secret; the bootstrap step fills these from local\n"
        "# config).\n"
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
  description = "Resource group the ACR and identities live in."
  type        = string
}

variable "aks_cluster_name" {
  description = "Name of the EXISTING AKS cluster the workload deploys to (referenced, not created)."
  type        = string
}

variable "aks_resource_group_name" {
  description = "Resource group of the existing AKS cluster."
  type        = string
}

variable "acr_sku" {
  description = "SKU for the Azure Container Registry (Basic/Standard/Premium)."
  type        = string
  default     = "Standard"
}

variable "service_account_namespace" {
  description = "Kubernetes namespace of the workload ServiceAccount (workload identity binding)."
  type        = string
  default     = "default"
}

variable "service_account_name" {
  description = "Kubernetes ServiceAccount name the workload identity is bound to."
  type        = string
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
    """Render ``main.tf`` — ACR + AKS ref + managed identity + workload/CI federation.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The region is referenced via ``var.location`` (whose default the IR drives in
 ``variables.tf``), so no region literal is hard-coded in a resource. The AKS
 cluster is REFERENCED (a ``data`` source), never created. Image pulls use the
 AKS→ACR role assignment, the pod federates to a user-assigned identity via the
 cluster's OIDC issuer (AKS Workload Identity), and the CI pipeline federates via
 its own identity + OIDC credential (keyless — no client secret committed, R6).
 """
    prefix = _sanitize_name(project_name)
    acr_name = _sanitize_acr_name(project_name)

    return f"""\
# azure-aks backing infrastructure (generated — design §11.9: AKS refs + workload
# identity). References an EXISTING AKS cluster, provisions an ACR, a user-assigned
# managed identity bound to the workload ServiceAccount via AKS Workload Identity,
# and a user-assigned identity + OIDC federated credential for keyless CI deploys.
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

# --- Resource group the ACR + identities land in ------------------------------
resource "azurerm_resource_group" "aks" {{
  name     = var.resource_group_name
  location = var.location
}}

# --- Azure Container Registry: holds the image the pipeline builds + pushes -----
resource "azurerm_container_registry" "app" {{
  name                = local.acr_name
  resource_group_name = azurerm_resource_group.aks.name
  location            = azurerm_resource_group.aks.location
  sku                 = var.acr_sku
  admin_enabled       = false
}}

# --- Reference the EXISTING AKS cluster (not created — client-owned) -----------
# The cluster's OIDC issuer URL drives the workload-identity federated credential.
data "azurerm_kubernetes_cluster" "aks" {{
  name                = var.aks_cluster_name
  resource_group_name = var.aks_resource_group_name
}}

# --- Let the cluster pull from the ACR (no committed registry credential, R6) ---
resource "azurerm_role_assignment" "aks_acr_pull" {{
  scope                            = azurerm_container_registry.app.id
  role_definition_name             = "AcrPull"
  principal_id                     = data.azurerm_kubernetes_cluster.aks.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}}

# --- Workload identity: the app pod federates to this Azure AD identity ---------
resource "azurerm_user_assigned_identity" "workload" {{
  name                = "${{local.name_prefix}}-workload"
  resource_group_name = azurerm_resource_group.aks.name
  location            = azurerm_resource_group.aks.location
}}

# AKS Workload Identity binding: tie the identity to the Kubernetes ServiceAccount
# through the cluster's OIDC issuer so the pod gets Azure AD tokens with no secret.
resource "azurerm_federated_identity_credential" "workload" {{
  name                = "${{local.name_prefix}}-workload-fic"
  resource_group_name = azurerm_resource_group.aks.name
  parent_id           = azurerm_user_assigned_identity.workload.id
  audience            = [var.oidc_audience]
  issuer              = data.azurerm_kubernetes_cluster.aks.oidc_issuer_url
  subject             = "system:serviceaccount:${{var.service_account_namespace}}:${{var.service_account_name}}"
}}

# --- CI identity + OIDC federated credential for keyless CI deploy -------------
# The CI pipeline exchanges its OIDC token for Azure access via this identity — no
# client secret is created or committed (the R6/OIDC story). The CI identity can
# push images to the ACR. Issuer + subject are variables the client fills.
resource "azurerm_user_assigned_identity" "ci_deploy" {{
  name                = "${{local.name_prefix}}-ci-deploy"
  resource_group_name = azurerm_resource_group.aks.name
  location            = azurerm_resource_group.aks.location
}}

resource "azurerm_role_assignment" "ci_acr_push" {{
  scope                = azurerm_container_registry.app.id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}}

resource "azurerm_federated_identity_credential" "ci_deploy" {{
  name                = "${{local.name_prefix}}-ci-oidc"
  resource_group_name = azurerm_resource_group.aks.name
  parent_id           = azurerm_user_assigned_identity.ci_deploy.id
  audience            = [var.oidc_audience]
  issuer              = var.oidc_issuer_url
  subject             = var.oidc_subject
}}
"""


def render_outputs_tf() -> str:
    """Render ``outputs.tf`` — the handles the pipeline/bootstrap need downstream.

 These are the non-secret identifiers the verbs reference (the ACR login server
 the package verb pushes to, the workload identity client id the ServiceAccount
 annotation uses, and the CI identity client id the OIDC login uses). Outputs make
 the module composable without re-deriving names elsewhere. No secret is output.
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

output "workload_identity_client_id" {
  description = "User-assigned identity client id the workload ServiceAccount is annotated with."
  value       = azurerm_user_assigned_identity.workload.client_id
}

output "ci_deploy_identity_client_id" {
  description = "User-assigned identity client id the CI pipeline logs in with via OIDC."
  value       = azurerm_user_assigned_identity.ci_deploy.client_id
}

output "aks_cluster_name" {
  description = "Name of the referenced AKS cluster the workload deploys to."
  value       = data.azurerm_kubernetes_cluster.aks.name
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
# Example tfvars for the azure-aks backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

location            = "{region_value}"
project_name        = "{prefix}"
resource_group_name = "{prefix}-rg"

# The EXISTING AKS cluster the workload deploys to (referenced, not created).
aks_cluster_name        = "{prefix}-aks"
aks_resource_group_name = "{prefix}-aks-rg"

# Registry tier.
acr_sku = "Standard"

# The Kubernetes ServiceAccount the workload identity is bound to.
service_account_namespace = "default"
service_account_name      = "{prefix}-sa"

# CI OIDC trust — scope the subject to your repo/branch.
oidc_issuer_url = "https://token.actions.githubusercontent.com"
oidc_subject    = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_audience   = "{_DEFAULT_OIDC_AUDIENCE}"
"""
