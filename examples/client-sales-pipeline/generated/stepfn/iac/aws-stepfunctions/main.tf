# aws-stepfunctions backing infrastructure (generated — design §11.9: SFN + role).
#
# State-machine orchestration shape: NO container image, NO ECR, NO ECS — the
# deployable is an aws_sfn_state_machine running an Amazon States Language body.
#
# Parameterised entirely by variables (see variables.tf). No literal account ids,
# ARNs, or secrets appear here (R6). Resource names derive from the IR project
# name ('sales-orchestrator') for a stable, readable layout (R5.3).

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Account id is resolved at apply time — never committed as a literal (R6).
data "aws_caller_identity" "current" {}

locals {
  name_prefix = "sales-orchestrator"
  account_id  = data.aws_caller_identity.current.account_id
}

# --- S3: holds the versioned ASL definition + task artifacts (package verb) -----
resource "aws_s3_bucket" "artifacts" {
  bucket = "${local.name_prefix}-${local.account_id}-sfn-artifacts"
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- CloudWatch log group for the state machine (optional) ---------------------
resource "aws_cloudwatch_log_group" "app" {
  count             = var.enable_logging ? 1 : 0
  name              = "/aws-stepfunctions/${local.name_prefix}"
  retention_in_days = 30
}

# --- IAM: state-machine execution role (assumed by the service) -----------------
data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn_exec" {
  name               = "${local.name_prefix}-sfn-exec"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

# Scoped inline policy: invoke the task Lambda(s) and start Glue jobs the states
# call, plus (optionally) CloudWatch Logs delivery when logging is enabled. The
# Lambda ARNs the state machine may invoke are a variable so no account specific
# is hard-coded; an empty list grants no Lambda invoke (safe default).
data "aws_iam_policy_document" "sfn_exec_permissions" {
  dynamic "statement" {
    for_each = length(var.task_lambda_arns) > 0 ? [1] : []
    content {
      sid       = "InvokeTaskLambdas"
      effect    = "Allow"
      actions   = ["lambda:InvokeFunction"]
      resources = var.task_lambda_arns
    }
  }

  statement {
    sid    = "RunGlueJobs"
    effect = "Allow"
    actions = [
      "glue:StartJobRun",
      "glue:GetJobRun",
      "glue:GetJobRuns",
      "glue:BatchStopJobRun",
    ]
    resources = ["*"]
  }

  dynamic "statement" {
    for_each = var.enable_logging ? [1] : []
    content {
      sid    = "StateMachineLogging"
      effect = "Allow"
      actions = [
        "logs:CreateLogDelivery",
        "logs:GetLogDelivery",
        "logs:UpdateLogDelivery",
        "logs:DeleteLogDelivery",
        "logs:ListLogDeliveries",
        "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies",
        "logs:DescribeLogGroups",
      ]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "sfn_exec" {
  name   = "${local.name_prefix}-sfn-exec"
  role   = aws_iam_role.sfn_exec.id
  policy = data.aws_iam_policy_document.sfn_exec_permissions.json
}

# --- Step Functions: the deployable workload (deploy verb target) --------------
# A minimal starting definition the deploy verb updates out of band.
resource "aws_sfn_state_machine" "app" {
  name     = "${local.name_prefix}-sfn"
  role_arn = aws_iam_role.sfn_exec.arn
  type     = var.state_machine_type

  definition = jsonencode({
    Comment = "Starting state machine for ${local.name_prefix} (deploy verb updates it)."
    StartAt = "Start"
    States = {
      Start = {
        Type   = "Pass"
        Result = { status = "ok" }
        Next   = "Done"
      }
      Done = {
        Type = "Succeed"
      }
    }
  })

  dynamic "logging_configuration" {
    for_each = var.enable_logging ? [1] : []
    content {
      log_destination        = "${aws_cloudwatch_log_group.app[0].arn}:*"
      include_execution_data = true
      level                  = "ALL"
    }
  }

  lifecycle {
    # The deploy verb updates the definition out of band; ignore drift on it.
    ignore_changes = [definition]
  }
}

# --- CI deploy identity (OIDC keyless OR static keys; see var.use_oidc) --------
# All OIDC resources are gated by count so the key-auth path (use_oidc = false)
# provisions none of them — useful where an org SCP denies creating an OIDC
# provider. Under key auth the pipeline uses access-key GitHub secrets instead.
resource "aws_iam_openid_connect_provider" "ci" {
  count          = var.use_oidc ? 1 : 0
  url            = var.oidc_provider_url
  client_id_list = var.oidc_client_id_list
  # Thumbprint is validated by AWS for well-known providers; kept as a variable-free
  # well-known value so the module applies without committing account specifics.
  thumbprint_list = ["ffffffffffffffffffffffffffffffffffffffff"]
}

data "aws_iam_policy_document" "ci_deploy_assume" {
  count = var.use_oidc ? 1 : 0
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.ci[0].arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${replace(var.oidc_provider_url, "https://", "")}:aud"
      values   = var.oidc_client_id_list
    }

    condition {
      test     = "StringLike"
      variable = "${replace(var.oidc_provider_url, "https://", "")}:sub"
      values   = [var.oidc_subject]
    }
  }
}

resource "aws_iam_role" "ci_deploy" {
  count              = var.use_oidc ? 1 : 0
  name               = "${local.name_prefix}-ci-deploy"
  assume_role_policy = data.aws_iam_policy_document.ci_deploy_assume[0].json
}

# Deploy role can stage the definition and create/update/start the state machine —
# scoped to this project.
data "aws_iam_policy_document" "ci_deploy_permissions" {
  count = var.use_oidc ? 1 : 0
  statement {
    sid    = "S3ArtifactUpload"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]
  }

  statement {
    sid    = "StepFunctionsDeploy"
    effect = "Allow"
    actions = [
      "states:CreateStateMachine",
      "states:UpdateStateMachine",
      "states:DescribeStateMachine",
      "states:ListStateMachines",
      "states:StartExecution",
      "states:DescribeExecution",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PassStateMachineRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.sfn_exec.arn]
  }
}

resource "aws_iam_role_policy" "ci_deploy" {
  count  = var.use_oidc ? 1 : 0
  name   = "${local.name_prefix}-ci-deploy"
  role   = aws_iam_role.ci_deploy[0].id
  policy = data.aws_iam_policy_document.ci_deploy_permissions[0].json
}
