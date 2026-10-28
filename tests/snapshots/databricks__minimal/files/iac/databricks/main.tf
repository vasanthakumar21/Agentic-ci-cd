# databricks backing infrastructure (generated — design §8b: workspace, Unity
# Catalog, job). This is the shape MOST unlike aws-ecs: NO container image, NO ECR,
# NO ECS — the deployable is a Databricks JOB running an uploaded wheel/notebook.
#
# Scope (task 9.2): this module wires a JOB + an OPTIONAL Unity Catalog schema INTO
# an EXISTING workspace. Creating the workspace itself is an account-level step
# (account creds / cloud tenancy) kept out of scope on purpose (R7.1 — honest about
# limits); see README.md. The provider below targets the existing workspace by
# reference (host + token are variables, never literals — R6).
#
# Parameterised entirely by variables (see variables.tf). No literal workspace URL,
# token, or account specifics appear here (R6). Resource names derive from the IR
# project name ('minimal-svc') for a stable, readable layout (R5.3).

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    databricks = {
      source  = "databricks/databricks"
      version = ">= 1.40.0"
    }
  }
}

# --- Workspace wiring -----------------------------------------------------------
# The provider authenticates against an EXISTING workspace. Host + token are
# referenced, never committed as literals (R6); the token var is sensitive.
provider "databricks" {
  host  = var.databricks_host
  token = var.databricks_token
}

locals {
  name_prefix = "minimal-svc"
}

# --- Unity Catalog (OPTIONAL) ---------------------------------------------------
# A schema the job writes into, gated by var.enable_unity_catalog so workspaces
# WITHOUT Unity Catalog aren't forced into it (set the flag false -> no schema).
# Created in an EXISTING catalog (var.unity_catalog); this module does not create
# the catalog or metastore (account-level, out of scope — see README.md).
resource "databricks_schema" "app" {
  count        = var.enable_unity_catalog ? 1 : 0
  catalog_name = var.unity_catalog
  name         = "minimal_svc"
  comment      = "Schema for ${local.name_prefix} job output (generated)."
}

# --- The Databricks JOB the deploy verb creates/updates and verify runs ---------
# Runs the uploaded wheel (var.artifact_path) on an ephemeral job cluster. NO image.
# Schedule + run-as are optional (empty defaults) so nothing is imposed implicitly.
resource "databricks_job" "app" {
  name = "${local.name_prefix}-job"

  # Optional run-as identity (omitted when var.run_as_user_name is empty).
  dynamic "run_as" {
    for_each = var.run_as_user_name != "" ? [var.run_as_user_name] : []
    content {
      user_name = run_as.value
    }
  }

  # Optional schedule (omitted when var.job_schedule_cron is empty — run on demand).
  dynamic "schedule" {
    for_each = var.job_schedule_cron != "" ? [var.job_schedule_cron] : []
    content {
      quartz_cron_expression = schedule.value
      timezone_id            = var.job_schedule_timezone
      pause_status           = "UNPAUSED"
    }
  }

  job_cluster {
    job_cluster_key = "${local.name_prefix}-cluster"
    new_cluster {
      spark_version = var.spark_version
      node_type_id  = var.node_type_id
      num_workers   = var.num_workers
    }
  }

  task {
    task_key        = "${local.name_prefix}-task"
    job_cluster_key = "${local.name_prefix}-cluster"

    python_wheel_task {
      package_name = var.package_name
      entry_point  = var.entry_point
    }

    library {
      whl = var.artifact_path
    }
  }
}
