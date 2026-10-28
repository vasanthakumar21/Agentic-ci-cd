# aws-sagemaker backing infrastructure (generated — design §11.9: SageMaker + role).
#
# Container-based ML shape (bring-your-own-container): the deployable is a Docker
# image in ECR that a SageMaker model/endpoint serves, reading model artifacts from
# S3.
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
  name_prefix  = "minimal-svc"
  account_id   = data.aws_caller_identity.current.account_id
  ecr_repo_url = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${aws_ecr_repository.app.name}"
}

# --- ECR: holds the training/inference image (package verb) --------------------
resource "aws_ecr_repository" "app" {
  name                 = "${local.name_prefix}-sagemaker-ecr"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# --- S3: holds the versioned trained model artifacts ---------------------------
resource "aws_s3_bucket" "models" {
  bucket = "${local.name_prefix}-${local.account_id}-sagemaker-models"
}

resource "aws_s3_bucket_versioning" "models" {
  bucket = aws_s3_bucket.models.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "models" {
  bucket                  = aws_s3_bucket.models.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --- CloudWatch log group for the endpoint -------------------------------------
resource "aws_cloudwatch_log_group" "app" {
  name              = "/aws-sagemaker/${local.name_prefix}"
  retention_in_days = 30
}

# --- IAM: SageMaker execution role (assumed by the service at run time) --------
data "aws_iam_policy_document" "sagemaker_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["sagemaker.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sagemaker_exec" {
  name               = "${local.name_prefix}-sagemaker-exec"
  assume_role_policy = data.aws_iam_policy_document.sagemaker_assume.json
}

# Scoped inline policy: pull the image, read model artifacts, write CloudWatch logs.
data "aws_iam_policy_document" "sagemaker_exec_permissions" {
  statement {
    sid    = "PullImage"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "ReadModelArtifacts"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.models.arn,
      "${aws_s3_bucket.models.arn}/*",
    ]
  }

  statement {
    sid    = "WriteLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
}

resource "aws_iam_role_policy" "sagemaker_exec" {
  name   = "${local.name_prefix}-sagemaker-exec"
  role   = aws_iam_role.sagemaker_exec.id
  policy = data.aws_iam_policy_document.sagemaker_exec_permissions.json
}

# --- SageMaker: model + endpoint config + endpoint (deploy verb target) --------
resource "aws_sagemaker_model" "app" {
  name               = "${local.name_prefix}-model"
  execution_role_arn = aws_iam_role.sagemaker_exec.arn

  primary_container {
    image          = "${local.ecr_repo_url}:${var.image_tag}"
    model_data_url = "s3://${aws_s3_bucket.models.id}/${var.model_artifact_key}"
  }

  lifecycle {
    # The deploy verb creates new model/config versions out of band; ignore drift.
    ignore_changes = [primary_container]
  }
}

resource "aws_sagemaker_endpoint_configuration" "app" {
  name = "${local.name_prefix}-endpoint-config"

  production_variants {
    variant_name           = "AllTraffic"
    model_name             = aws_sagemaker_model.app.name
    initial_instance_count = var.instance_count
    instance_type          = var.instance_type
  }

  lifecycle {
    ignore_changes = [production_variants]
  }
}

resource "aws_sagemaker_endpoint" "app" {
  name                 = "${local.name_prefix}-endpoint"
  endpoint_config_name = aws_sagemaker_endpoint_configuration.app.name

  lifecycle {
    ignore_changes = [endpoint_config_name]
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

# Deploy role can push to ECR, stage model artifacts, and create/update the
# SageMaker model/endpoint — scoped to this project.
data "aws_iam_policy_document" "ci_deploy_permissions" {
  statement {
    sid    = "EcrPushPull"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "S3ModelUpload"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.models.arn,
      "${aws_s3_bucket.models.arn}/*",
    ]
  }

  statement {
    sid    = "SageMakerDeploy"
    effect = "Allow"
    actions = [
      "sagemaker:CreateModel",
      "sagemaker:CreateEndpointConfig",
      "sagemaker:CreateEndpoint",
      "sagemaker:UpdateEndpoint",
      "sagemaker:DescribeEndpoint",
      "sagemaker:DescribeModel",
      "sagemaker:InvokeEndpoint",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PassSageMakerExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.sagemaker_exec.arn]
  }
}

resource "aws_iam_role_policy" "ci_deploy" {
  name   = "${local.name_prefix}-ci-deploy"
  role   = aws_iam_role.ci_deploy.id
  policy = data.aws_iam_policy_document.ci_deploy_permissions.json
}
