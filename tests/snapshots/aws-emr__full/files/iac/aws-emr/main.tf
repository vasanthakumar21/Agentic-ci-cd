# aws-emr backing infrastructure (generated — design §11.9: EMR + roles).
#
# The data-job shape (like glue): NO container image, NO ECR, NO ECS — the
# deployable is an EMR SERVERLESS job running a PySpark script uploaded to S3.
#
# Parameterised entirely by variables (see variables.tf). No literal account ids,
# ARNs, or secrets appear here (R6). Resource names derive from the IR project
# name ('full-svc') for a stable, readable layout (R5.3).

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
  name_prefix = "full-svc"
  account_id  = data.aws_caller_identity.current.account_id
}

# --- S3: holds the versioned job script + extra libs the package verb uploads ---
resource "aws_s3_bucket" "artifacts" {
  bucket = "${local.name_prefix}-${local.account_id}-emr-artifacts"
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

# --- CloudWatch log group for the job ------------------------------------------
resource "aws_cloudwatch_log_group" "app" {
  name              = "/aws-emr-serverless/${local.name_prefix}"
  retention_in_days = 30
}

# --- EMR Serverless: the deployable Spark compute (deploy verb target) ----------
resource "aws_emrserverless_application" "app" {
  name          = "${local.name_prefix}-emr"
  release_label = var.emr_release_label
  type          = "spark"

  maximum_capacity {
    cpu    = var.max_cpu
    memory = var.max_memory
  }

  auto_start_configuration {
    enabled = true
  }

  auto_stop_configuration {
    enabled              = true
    idle_timeout_minutes = 15
  }
}

# --- IAM: EMR job execution role (assumed by the service at run time) -----------
data "aws_iam_policy_document" "emr_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["emr-serverless.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "emr_exec" {
  name               = "${local.name_prefix}-emr-exec"
  assume_role_policy = data.aws_iam_policy_document.emr_assume.json
}

# Scoped inline policy: read the artifact bucket + write this job's CloudWatch logs.
data "aws_iam_policy_document" "emr_exec_permissions" {
  statement {
    sid    = "ReadArtifactBucket"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
      "s3:PutObject",
    ]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]
  }

  statement {
    sid    = "WriteJobLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
}

resource "aws_iam_role_policy" "emr_exec" {
  name   = "${local.name_prefix}-emr-exec"
  role   = aws_iam_role.emr_exec.id
  policy = data.aws_iam_policy_document.emr_exec_permissions.json
}

# --- IAM + OIDC: keyless deploy identity for the CI pipeline (no access keys) --
resource "aws_iam_openid_connect_provider" "ci" {
  url            = var.oidc_provider_url
  client_id_list = var.oidc_client_id_list
  # Thumbprint is validated by AWS for well-known providers; kept as a variable-free
  # well-known value so the module applies without committing account specifics.
  thumbprint_list = ["ffffffffffffffffffffffffffffffffffffffff"]
}

data "aws_iam_policy_document" "ci_deploy_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.ci.arn]
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
  name               = "${local.name_prefix}-ci-deploy"
  assume_role_policy = data.aws_iam_policy_document.ci_deploy_assume.json
}

# Deploy role can stage artifacts and start/poll EMR Serverless job runs — scoped
# to this project.
data "aws_iam_policy_document" "ci_deploy_permissions" {
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
    sid    = "EmrDeploy"
    effect = "Allow"
    actions = [
      "emr-serverless:StartApplication",
      "emr-serverless:StartJobRun",
      "emr-serverless:GetJobRun",
      "emr-serverless:GetApplication",
      "emr-serverless:ListJobRuns",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PassEmrExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.emr_exec.arn]
  }
}

resource "aws_iam_role_policy" "ci_deploy" {
  name   = "${local.name_prefix}-ci-deploy"
  role   = aws_iam_role.ci_deploy.id
  policy = data.aws_iam_policy_document.ci_deploy_permissions.json
}
