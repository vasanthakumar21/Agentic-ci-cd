# azure-container-apps backing infrastructure (generated — design §11.9: Container
# Apps + identity). Provisions a Container Apps environment, the Container App, an
# ACR, a user-assigned managed identity for image pulls, and a user-assigned
# identity + OIDC federated credential for keyless CI deploys.
#
# Parameterised entirely by variables (see variables.tf). No literal subscription
# id, tenant, or secret appears here (R6). Resource names derive from the IR project
# name ('minimal-svc') for a stable, readable layout (R5.3).

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
  name_prefix = "minimal-svc"
  acr_name    = "acrminimalsvc"
}

# --- Resource group: everything below lands here ------------------------------
resource "azurerm_resource_group" "app" {
  name     = var.resource_group_name
  location = var.location
}

# --- Azure Container Registry: holds the image the pipeline builds + pushes -----
resource "azurerm_container_registry" "app" {
  name                = local.acr_name
  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
  sku                 = var.acr_sku
  admin_enabled       = false
}

# --- Log Analytics workspace: the Container Apps environment log backend --------
resource "azurerm_log_analytics_workspace" "app" {
  name                = "${local.name_prefix}-logs"
  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

# --- Container Apps environment: the managed environment the app runs in --------
resource "azurerm_container_app_environment" "app" {
  name                       = "${local.name_prefix}-env"
  resource_group_name        = azurerm_resource_group.app.name
  location                   = azurerm_resource_group.app.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.app.id
}

# --- App identity: used to pull from the ACR (no committed registry secret, R6) -
resource "azurerm_user_assigned_identity" "app" {
  name                = "${local.name_prefix}-app"
  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
}

resource "azurerm_role_assignment" "app_acr_pull" {
  scope                = azurerm_container_registry.app.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# --- The Container App the deploy verb updates and verify polls -----------------
# Single-revision mode so the latest revision receives all traffic. The image is a
# placeholder on first apply; the deploy verb sets the real image out of band so the
# committed HCL carries no client image reference. The app pulls via its identity.
resource "azurerm_container_app" "app" {
  name                         = "${local.name_prefix}-app"
  resource_group_name          = azurerm_resource_group.app.name
  container_app_environment_id = azurerm_container_app_environment.app.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.app.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  ingress {
    external_enabled = true
    target_port      = var.container_port
    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    container {
      name   = local.name_prefix
      image  = var.initial_image
      cpu    = 0.25
      memory = "0.5Gi"
    }
  }

  lifecycle {
    # The deploy verb updates the running image out of band; ignore that drift.
    ignore_changes = [template[0].container[0].image]
  }
}

# --- CI identity + OIDC federated credential for keyless CI deploy -------------
# The CI pipeline exchanges its OIDC token for Azure access via this identity — no
# client secret is created or committed (the R6/OIDC story). It can push images to
# the ACR and update the Container App. Issuer + subject are variables the client
# fills from their CI provider.
resource "azurerm_user_assigned_identity" "ci_deploy" {
  name                = "${local.name_prefix}-ci-deploy"
  resource_group_name = azurerm_resource_group.app.name
  location            = azurerm_resource_group.app.location
}

resource "azurerm_role_assignment" "ci_acr_push" {
  scope                = azurerm_container_registry.app.id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}

resource "azurerm_role_assignment" "ci_app_contributor" {
  scope                = azurerm_container_app.app.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}

resource "azurerm_federated_identity_credential" "ci_deploy" {
  name                = "${local.name_prefix}-ci-oidc"
  resource_group_name = azurerm_resource_group.app.name
  parent_id           = azurerm_user_assigned_identity.ci_deploy.id
  audience            = [var.oidc_audience]
  issuer              = var.oidc_issuer_url
  subject             = var.oidc_subject
}
