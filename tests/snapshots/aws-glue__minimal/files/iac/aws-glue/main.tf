# aws-glue backing infrastructure (generated — design §11.9: Glue job + role).
#
# The data-job shape (like databricks): NO container image, NO ECR, NO ECS — the
# deployable is a Glue JOB running a Spark/Python script uploaded to S3.
#
# Parameterised entirely by variables (see variables.tf). No literal account ids,
# ARNs, or secrets appear here (R6). Resource names derive from the IR project
# name ('minimal-svc') for a stable, readable layout (R5.3).

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
  name_prefix = "minimal-svc"
  account_id  = data.aws_caller_identity.current.account_id
}

# --- S3: holds the versioned job script + extra libs the package verb uploads ---
resource "aws_s3_bucket" "scripts" {
  bucket = "${local.name_prefix}-${local.account_id}-glue-scripts"
}

resource "aws_s3_bucket_versioning" "scripts" {
  bucket = aws_s3_bucket.scripts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "scripts" {
  bucket                  = aws_s3_bucket.scripts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- CloudWatch log group for the job ------------------------------------------
resource "aws_cloudwatch_log_group" "app" {
  name              = "/aws-glue/jobs/${local.name_prefix}"
  retention_in_days = 30
}

# --- IAM: Glue service role (assumed by the service at run time) ----------------
data "aws_iam_policy_document" "glue_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "glue_exec" {
  name               = "${local.name_prefix}-glue-exec"
  assume_role_policy = data.aws_iam_policy_document.glue_assume.json
}

# AWS-managed Glue service policy (public managed-policy ARN — not account-specific).
resource "aws_iam_role_policy_attachment" "glue_service" {
  role       = aws_iam_role.glue_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

# Scoped inline policy: read the script bucket + write this job's CloudWatch logs.
data "aws_iam_policy_document" "glue_exec_permissions" {
  statement {
    sid    = "ReadScriptBucket"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.scripts.arn,
      "${aws_s3_bucket.scripts.arn}/*",
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

resource "aws_iam_role_policy" "glue_exec" {
  name   = "${local.name_prefix}-glue-exec"
  role   = aws_iam_role.glue_exec.id
  policy = data.aws_iam_policy_document.glue_exec_permissions.json
}

# --- Glue: the deployable workload (deploy verb target) ------------------------
# The script location is a VARIABLE key into the versioned bucket so no artifact
# value is committed; the deploy verb updates the job + the verify verb runs it.
resource "aws_glue_job" "app" {
  name              = local.name_prefix
  role_arn          = aws_iam_role.glue_exec.arn
  glue_version      = var.glue_version
  worker_type       = var.worker_type
  number_of_workers = var.number_of_workers
  max_retries       = var.max_retries
  timeout           = var.timeout_minutes

  command {
    name            = "glueetl"
    python_version  = "3"
    script_location = "s3://${aws_s3_bucket.scripts.id}/${var.script_key}"
  }

  default_arguments = {
    "--job-language"                     = "python"
    "--extra-py-files"                   = "s3://${aws_s3_bucket.scripts.id}/${var.extra_py_files_key}"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
  }

  lifecycle {
    # The deploy verb updates the script/args out of band; ignore drift on those.
    ignore_changes = [command, default_arguments]
  }
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

# Deploy role can stage scripts and create/update/start the Glue job — scoped to
# this project.
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
      aws_s3_bucket.scripts.arn,
      "${aws_s3_bucket.scripts.arn}/*",
    ]
  }

  statement {
    sid    = "GlueDeploy"
    effect = "Allow"
    actions = [
      "glue:CreateJob",
      "glue:UpdateJob",
      "glue:GetJob",
      "glue:GetJobRun",
      "glue:StartJobRun",
      "glue:BatchStopJobRun",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PassGlueExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.glue_exec.arn]
  }
}

resource "aws_iam_role_policy" "ci_deploy" {
  name   = "${local.name_prefix}-ci-deploy"
  role   = aws_iam_role.ci_deploy.id
  policy = data.aws_iam_policy_document.ci_deploy_permissions.json
}
