# aws-lambda backing infrastructure (generated — design §11.9: Lambda, IAM/OIDC).
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
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.0"
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
  is_image    = var.package_type == "Image"
}

# A tiny valid placeholder zip so the FIRST `terraform apply` can create the
# function before any real artifact exists in S3 (fixes first-run NoSuchKey). The
# CI pipeline's deploy verb replaces the code on every push; Terraform ignores
# that drift (see ignore_changes below). Deterministic, no network.
data "archive_file" "bootstrap" {
  count       = local.is_image ? 0 : 1
  type        = "zip"
  output_path = "${path.module}/.bootstrap/placeholder.zip"

  source {
    filename = "bootstrap.txt"
    content  = "placeholder — replaced by the CI deploy on first push"
  }
}

# --- S3: holds the versioned deployment zip the package verb uploads -----------
resource "aws_s3_bucket" "artifacts" {
  bucket = "${local.name_prefix}-${local.account_id}-lambda-artifacts"
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

# --- CloudWatch log group for the function (optional) --------------------------
# Off by default: Lambda auto-creates its own log group on first invoke, and some
# org policies restrict the CloudWatch Logs management APIs. Enable to manage the
# group's retention explicitly via Terraform.
resource "aws_cloudwatch_log_group" "app" {
  count             = var.enable_logging ? 1 : 0
  name              = "/aws/lambda/${local.name_prefix}"
  retention_in_days = 30
}

# --- IAM: Lambda execution role (assumed by the service at run time) -----------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${local.name_prefix}-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# --- Lambda: the deployable workload (deploy verb target) ----------------------
# The source is a VARIABLE (S3 object for Zip, image uri for Image) so no artifact
# value is committed; the deploy verb publishes new versions and moves the alias.
resource "aws_lambda_function" "app" {
  function_name = local.name_prefix
  role          = aws_iam_role.lambda_exec.arn
  package_type  = var.package_type
  memory_size   = var.memory_size
  timeout       = var.timeout_seconds
  publish       = true

  # Zip packaging: CREATE with a local placeholder zip so the first apply works
  # with no pre-existing S3 object. The CI deploy verb then updates the code from
  # S3 on every push; Terraform ignores that drift (ignore_changes below).
  filename         = local.is_image ? null : data.archive_file.bootstrap[0].output_path
  source_code_hash = local.is_image ? null : data.archive_file.bootstrap[0].output_base64sha256
  runtime          = local.is_image ? null : var.lambda_runtime
  handler          = local.is_image ? null : var.lambda_handler

  # Container packaging (optional): point at an immutable image digest.
  image_uri = local.is_image ? var.image_uri : null

  depends_on = [aws_cloudwatch_log_group.app]

  lifecycle {
    # The deploy verb updates code/image out of band; ignore drift on those.
    ignore_changes = [filename, source_code_hash, s3_bucket, s3_key, image_uri]
  }
}

# NOTE: aws_lambda_function.depends_on tolerates an empty log-group list when
# logging is disabled (count = 0 yields an empty tuple, a valid dependency set).

resource "aws_lambda_alias" "live" {
  name             = var.alias_name
  function_name    = aws_lambda_function.app.function_name
  function_version = aws_lambda_function.app.version
}

# --- CI deploy identity --------------------------------------------------------
# Two auth modes, selected by var.use_oidc:
#   * use_oidc = true  -> keyless OIDC: create a GitHub-OIDC provider + a role the
#                         pipeline assumes via a web-identity token (best practice).
#   * use_oidc = false -> static keys: NO OIDC provider / federated role is created
#                         (useful where an org SCP denies iam:CreateOpenIDConnect-
#                         Provider). The pipeline authenticates with a deploy user's
#                         access keys stored as GitHub secrets instead.
# All OIDC resources below are gated by count so the key-auth path provisions none
# of them — the SCP is never triggered.
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

# Deploy role can stage artifacts and roll the function — scoped to this project.
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
    sid    = "EcrPullForImageLambda"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:PutImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "LambdaDeploy"
    effect = "Allow"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
      "lambda:PublishVersion",
      "lambda:UpdateAlias",
      "lambda:GetAlias",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:InvokeFunction",
    ]
    resources = [
      aws_lambda_function.app.arn,
      "${aws_lambda_function.app.arn}:*",
    ]
  }

  statement {
    sid       = "PassExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.lambda_exec.arn]
  }
}

resource "aws_iam_role_policy" "ci_deploy" {
  count  = var.use_oidc ? 1 : 0
  name   = "${local.name_prefix}-ci-deploy"
  role   = aws_iam_role.ci_deploy[0].id
  policy = data.aws_iam_policy_document.ci_deploy_permissions[0].json
}
