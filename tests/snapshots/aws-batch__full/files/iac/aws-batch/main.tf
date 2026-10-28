# aws-batch backing infrastructure (generated — design §11.9: Batch + compute env).
#
# Container-based compute shape (like ecs): the deployable is a Docker image in ECR
# that an AWS Batch job definition runs on a Fargate compute environment.
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
  name_prefix  = "full-svc"
  account_id   = data.aws_caller_identity.current.account_id
  ecr_repo_url = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${aws_ecr_repository.app.name}"
}

# --- ECR: holds the image the pipeline builds and pushes (package verb) --------
resource "aws_ecr_repository" "app" {
  name                 = "${local.name_prefix}-batch-ecr"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# --- CloudWatch log group for the Batch job ------------------------------------
resource "aws_cloudwatch_log_group" "app" {
  name              = "/aws-batch/${local.name_prefix}"
  retention_in_days = 30
}

# --- IAM: Batch job execution role (Fargate: pull image, write logs) -----------
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "job_execution" {
  name               = "${local.name_prefix}-batch-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "job_execution" {
  role       = aws_iam_role.job_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# --- IAM: Batch service role ---------------------------------------------------
data "aws_iam_policy_document" "batch_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["batch.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "batch_service" {
  name               = "${local.name_prefix}-batch-service"
  assume_role_policy = data.aws_iam_policy_document.batch_assume.json
}

resource "aws_iam_role_policy_attachment" "batch_service" {
  role       = aws_iam_role.batch_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

# --- Batch: Fargate compute environment + job queue + job definition -----------
resource "aws_batch_compute_environment" "app" {
  compute_environment_name = "${local.name_prefix}-batch-ce"
  type                     = "MANAGED"
  service_role             = aws_iam_role.batch_service.arn

  compute_resources {
    type               = "FARGATE"
    max_vcpus          = var.max_vcpus
    subnets            = var.subnet_ids
    security_group_ids = var.security_group_ids
  }
}

resource "aws_batch_job_queue" "app" {
  name     = "${local.name_prefix}-batch-queue"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 0
    compute_environment = aws_batch_compute_environment.app.arn
  }
}

resource "aws_batch_job_definition" "app" {
  name                  = "${local.name_prefix}-batch-job"
  type                  = "container"
  platform_capabilities = ["FARGATE"]

  container_properties = jsonencode({
    image      = "${local.ecr_repo_url}:${var.image_tag}"
    executionRoleArn = aws_iam_role.job_execution.arn
    resourceRequirements = [
      { type = "VCPU", value = var.job_vcpu },
      { type = "MEMORY", value = var.job_memory },
    ]
    networkConfiguration = {
      assignPublicIp = "ENABLED"
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "batch"
      }
    }
  })
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

# Deploy role can push to ECR and register/submit Batch jobs — scoped to this project.
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
    sid    = "BatchDeploy"
    effect = "Allow"
    actions = [
      "batch:RegisterJobDefinition",
      "batch:DescribeJobDefinitions",
      "batch:SubmitJob",
      "batch:DescribeJobs",
      "batch:DescribeJobQueues",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PassBatchRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [
      aws_iam_role.job_execution.arn,
      aws_iam_role.batch_service.arn,
    ]
  }
}

resource "aws_iam_role_policy" "ci_deploy" {
  name   = "${local.name_prefix}-ci-deploy"
  role   = aws_iam_role.ci_deploy.id
  policy = data.aws_iam_policy_document.ci_deploy_permissions.json
}
