# azure-functions backing infrastructure (generated — design §8b: Function App +
# identity). Provisions a Function App, its backing storage account, an App Service
# plan, and a user-assigned identity with an OIDC federated credential for keyless
# CI deploys.
#
# Parameterised entirely by variables (see variables.tf). No literal subscription
# id, tenant, connection string, or secret appears here (R6). Resource names derive
# from the IR project name ('full-svc') for a stable, readable layout (R5.3).

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
  name_prefix  = "full-svc"
  storage_name = "stfullsvcfunc"
}

# --- Resource group: everything below lands here ------------------------------
resource "azurerm_resource_group" "func" {
  name     = var.resource_group_name
  location = var.location
}

# --- Storage account: the Function App runtime's required backing store --------
# The Function App reads this by resource reference — no literal connection string
# or access key is ever committed (R6).
resource "azurerm_storage_account" "func" {
  name                     = local.storage_name
  resource_group_name      = azurerm_resource_group.func.name
  location                 = azurerm_resource_group.func.location
  account_tier             = var.storage_account_tier
  account_replication_type = var.storage_replication_type
}

# --- App Service plan the Function App runs on (Y1 = consumption by default) ---
resource "azurerm_service_plan" "func" {
  name                = "${local.name_prefix}-plan"
  resource_group_name = azurerm_resource_group.func.name
  location            = azurerm_resource_group.func.location
  os_type             = "Linux"
  sku_name            = var.service_plan_sku
}

# --- The Function App the deploy verb publishes into and verify smoke-tests ----
# Given a system-assigned managed identity so it authenticates to other Azure
# resources without a committed secret (R6). The storage account is wired by
# resource reference (name + access key come from the resource, not a literal).
resource "azurerm_linux_function_app" "func" {
  name                       = "${local.name_prefix}-func"
  resource_group_name        = azurerm_resource_group.func.name
  location                   = azurerm_resource_group.func.location
  service_plan_id            = azurerm_service_plan.func.id
  storage_account_name       = azurerm_storage_account.func.name
  storage_account_access_key = azurerm_storage_account.func.primary_access_key

  identity {
    type = "SystemAssigned"
  }

  site_config {
    application_stack {
      python_version = var.runtime_version
    }
  }
}

# --- User-assigned identity + OIDC federated credential for keyless CI deploy --
# The CI pipeline exchanges its OIDC token for Azure access via this identity — no
# client secret is created or committed (the R6/OIDC story). Issuer + subject are
# variables the client fills from their CI provider.
resource "azurerm_user_assigned_identity" "ci_deploy" {
  name                = "${local.name_prefix}-ci-deploy"
  resource_group_name = azurerm_resource_group.func.name
  location            = azurerm_resource_group.func.location
}

resource "azurerm_federated_identity_credential" "ci_deploy" {
  name                = "${local.name_prefix}-ci-oidc"
  resource_group_name = azurerm_resource_group.func.name
  parent_id           = azurerm_user_assigned_identity.ci_deploy.id
  audience            = [var.oidc_audience]
  issuer              = var.oidc_issuer_url
  subject             = var.oidc_subject
}
