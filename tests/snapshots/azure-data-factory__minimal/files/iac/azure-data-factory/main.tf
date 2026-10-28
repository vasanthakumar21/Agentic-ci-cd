# azure-data-factory backing infrastructure (generated — design §11.9: ADF + identity).
#
# The data-job shape (like databricks): NO container image, NO ACR, NO ECS — the
# deployable is an Azure Data Factory PIPELINE (ARM/JSON definitions) the deploy verb
# upserts and the verify verb triggers + polls.
#
# Parameterised entirely by variables (see variables.tf). No literal subscription
# id, tenant, connection string, or secret appears here (R6). Resource names derive
# from the IR project name ('minimal-svc') for a stable, readable layout (R5.3).

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
  name_prefix  = "minimal-svc"
  storage_name = "stminimalsvcadf"
}

# --- Resource group the factory + storage + identities land in -----------------
resource "azurerm_resource_group" "adf" {
  name     = var.resource_group_name
  location = var.location
}

# --- ADLS Gen2 data lake: the package verb uploads ADF definitions/artifacts ----
# Activities read artifacts / write output via the role assignment below — no
# account key is ever committed (R6).
resource "azurerm_storage_account" "lake" {
  name                     = local.storage_name
  resource_group_name      = azurerm_resource_group.adf.name
  location                 = azurerm_resource_group.adf.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  is_hns_enabled           = true
}

resource "azurerm_storage_data_lake_gen2_filesystem" "definitions" {
  name               = var.artifact_filesystem
  storage_account_id = azurerm_storage_account.lake.id
}

# --- The Data Factory the deploy verb upserts pipelines into --------------------
# A system-assigned managed identity lets linked services authenticate to storage
# without a committed connection string / key (R6).
resource "azurerm_data_factory" "app" {
  name                = "${local.name_prefix}-adf"
  resource_group_name = azurerm_resource_group.adf.name
  location            = azurerm_resource_group.adf.location

  identity {
    type = "SystemAssigned"
  }
}

# --- Starter pipeline so the factory is usable immediately ----------------------
# The deploy verb maintains the real pipeline out of band (from the committed JSON);
# this resource just ensures a pipeline of the expected name exists.
resource "azurerm_data_factory_pipeline" "app" {
  name            = "${local.name_prefix}-pipeline"
  data_factory_id = azurerm_data_factory.app.id
}

# --- Let the factory identity read/write the data lake (no key, R6) ------------
resource "azurerm_role_assignment" "adf_lake" {
  scope                = azurerm_storage_account.lake.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_data_factory.app.identity[0].principal_id
}

# --- CI identity + OIDC federated credential for keyless CI deploy -------------
# The CI pipeline exchanges its OIDC token for Azure access via this identity — no
# client secret is created or committed (the R6/OIDC story). It can upload
# definitions to the data lake and upsert/run ADF pipelines.
resource "azurerm_user_assigned_identity" "ci_deploy" {
  name                = "${local.name_prefix}-ci-deploy"
  resource_group_name = azurerm_resource_group.adf.name
  location            = azurerm_resource_group.adf.location
}

resource "azurerm_role_assignment" "ci_lake" {
  scope                = azurerm_storage_account.lake.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}

resource "azurerm_role_assignment" "ci_factory" {
  scope                = azurerm_data_factory.app.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.ci_deploy.principal_id
}

resource "azurerm_federated_identity_credential" "ci_deploy" {
  name                = "${local.name_prefix}-ci-oidc"
  resource_group_name = azurerm_resource_group.adf.name
  parent_id           = azurerm_user_assigned_identity.ci_deploy.id
  audience            = [var.oidc_audience]
  issuer              = var.oidc_issuer_url
  subject             = var.oidc_subject
}
