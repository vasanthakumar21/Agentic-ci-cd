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
# the IR project name ('full-svc') for a stable, readable layout (R5.3).

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
  storage_name = "stfullsvcsyn"
  pool_name    = "spfullsvc"
}

# --- Resource group the pool + storage + identities land in --------------------
resource "azurerm_resource_group" "synapse" {
  name     = var.resource_group_name
  location = var.location
}

# --- ADLS Gen2 data lake: the package verb uploads artifacts here ---------------
# Hierarchical namespace enabled (ADLS Gen2). Jobs read artifacts / write output via
# role assignments below — no account key is ever committed (R6).
resource "azurerm_storage_account" "lake" {
  name                     = local.storage_name
  resource_group_name      = azurerm_resource_group.synapse.name
  location                 = azurerm_resource_group.synapse.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  is_hns_enabled           = true
}

resource "azurerm_storage_data_lake_gen2_filesystem" "artifacts" {
  name               = var.artifact_filesystem
  storage_account_id = azurerm_storage_account.lake.id
}

# --- Reference the EXISTING Synapse workspace (not created — client-owned) ------
data "azurerm_synapse_workspace" "ws" {
  name                = var.synapse_workspace_name
  resource_group_name = var.synapse_resource_group_name
}

# --- Synapse Spark pool the Spark job definitions run on ------------------------
resource "azurerm_synapse_spark_pool" "app" {
  name                 = local.pool_name
  synapse_workspace_id = data.azurerm_synapse_workspace.ws.id
  node_size_family     = "MemoryOptimized"
  node_size            = var.node_size
  node_count           = var.node_count
  spark_version        = var.spark_version
}

# --- Let the Synapse workspace identity read/write the data lake (no key, R6) ---
resource "azurerm_role_assignment" "ws_lake" {
  scope                = azurerm_storage_account.lake.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_synapse_workspace.ws.identity[0].principal_id
}

# --- CI identity + OIDC federated credential for keyless CI deploy -------------
# The CI pipeline exchanges its OIDC token for Azure access via this identity — no
# client secret is created or committed (the R6/OIDC story). It can upload artifacts
# to the data lake and create/update/run Spark job definitions in the workspace.
resource "azurerm_user_assigned_identity" "ci_deploy" {
  name                = "${local.name_prefix}-ci-deploy"
  resource_group_name = azurerm_resource_group.synapse.name
  location            = azurerm_resource_group.synapse.location
}

resource "azurerm_role_assignment" "ci_lake" {
  scope                = azurerm_storage_account.lake.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}

resource "azurerm_role_assignment" "ci_workspace" {
  scope                = data.azurerm_synapse_workspace.ws.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}

resource "azurerm_federated_identity_credential" "ci_deploy" {
  name                = "${local.name_prefix}-ci-oidc"
  resource_group_name = azurerm_resource_group.synapse.name
  parent_id           = azurerm_user_assigned_identity.ci_deploy.id
  audience            = [var.oidc_audience]
  issuer              = var.oidc_issuer_url
  subject             = var.oidc_subject
}
