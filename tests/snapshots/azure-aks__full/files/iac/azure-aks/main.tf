# azure-aks backing infrastructure (generated — design §11.9: AKS refs + workload
# identity). References an EXISTING AKS cluster, provisions an ACR, a user-assigned
# managed identity bound to the workload ServiceAccount via AKS Workload Identity,
# and a user-assigned identity + OIDC federated credential for keyless CI deploys.
#
# Parameterised entirely by variables (see variables.tf). No literal subscription
# id, tenant, or secret appears here (R6). Resource names derive from the IR project
# name ('full-svc') for a stable, readable layout (R5.3).

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 3.80"
    }
  }
}

provider "azurerm" {
  features {}
}

locals {
  name_prefix = "full-svc"
  acr_name    = "acrfullsvc"
}

# --- Resource group the ACR + identities land in ------------------------------
resource "azurerm_resource_group" "aks" {
  name     = var.resource_group_name
  location = var.location
}

# --- Azure Container Registry: holds the image the pipeline builds + pushes -----
resource "azurerm_container_registry" "app" {
  name                = local.acr_name
  resource_group_name = azurerm_resource_group.aks.name
  location            = azurerm_resource_group.aks.location
  sku                 = var.acr_sku
  admin_enabled       = false
}

# --- Reference the EXISTING AKS cluster (not created — client-owned) -----------
# The cluster's OIDC issuer URL drives the workload-identity federated credential.
data "azurerm_kubernetes_cluster" "aks" {
  name                = var.aks_cluster_name
  resource_group_name = var.aks_resource_group_name
}

# --- Let the cluster pull from the ACR (no committed registry credential, R6) ---
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                            = azurerm_container_registry.app.id
  role_definition_name             = "AcrPull"
  principal_id                     = data.azurerm_kubernetes_cluster.aks.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}

# --- Workload identity: the app pod federates to this Azure AD identity ---------
resource "azurerm_user_assigned_identity" "workload" {
  name                = "${local.name_prefix}-workload"
  resource_group_name = azurerm_resource_group.aks.name
  location            = azurerm_resource_group.aks.location
}

# AKS Workload Identity binding: tie the identity to the Kubernetes ServiceAccount
# through the cluster's OIDC issuer so the pod gets Azure AD tokens with no secret.
resource "azurerm_federated_identity_credential" "workload" {
  name                = "${local.name_prefix}-workload-fic"
  resource_group_name = azurerm_resource_group.aks.name
  parent_id           = azurerm_user_assigned_identity.workload.id
  audience            = [var.oidc_audience]
  issuer              = data.azurerm_kubernetes_cluster.aks.oidc_issuer_url
  subject             = "system:serviceaccount:${var.service_account_namespace}:${var.service_account_name}"
}

# --- CI identity + OIDC federated credential for keyless CI deploy -------------
# The CI pipeline exchanges its OIDC token for Azure access via this identity — no
# client secret is created or committed (the R6/OIDC story). The CI identity can
# push images to the ACR. Issuer + subject are variables the client fills.
resource "azurerm_user_assigned_identity" "ci_deploy" {
  name                = "${local.name_prefix}-ci-deploy"
  resource_group_name = azurerm_resource_group.aks.name
  location            = azurerm_resource_group.aks.location
}

resource "azurerm_role_assignment" "ci_acr_push" {
  scope                = azurerm_container_registry.app.id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}

resource "azurerm_federated_identity_credential" "ci_deploy" {
  name                = "${local.name_prefix}-ci-oidc"
  resource_group_name = azurerm_resource_group.aks.name
  parent_id           = azurerm_user_assigned_identity.ci_deploy.id
  audience            = [var.oidc_audience]
  issuer              = var.oidc_issuer_url
  subject             = var.oidc_subject
}
