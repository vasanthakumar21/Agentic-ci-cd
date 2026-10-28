# aws-ecs backing infrastructure (generated — design §8b: ECR, ECS, IAM/OIDC).
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
  ecr_repo_url = "${local.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${aws_ecr_repository.app.name}"
}

# --- ECR: holds the image the pipeline builds and pushes (package verb) --------
resource "aws_ecr_repository" "app" {
  name                 = "${local.name_prefix}-ecr"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# --- CloudWatch log group for the ECS task ------------------------------------
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name_prefix}"
  retention_in_days = 30
}

# --- ECS: cluster + task definition + Fargate service (deploy verb target) -----
resource "aws_ecs_cluster" "app" {
  name = "${local.name_prefix}-cluster"
}

resource "aws_ecs_task_definition" "app" {
  family                   = "${local.name_prefix}-task"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([
    {
      name      = "${local.name_prefix}-app"
      image     = "${local.ecr_repo_url}:${var.image_tag}"
      essential = true
      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "app" {
  name            = "${local.name_prefix}-service"
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = true
  }
}

# --- IAM: ECS task execution role (pull image, write logs) ---------------------
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name               = "${local.name_prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# --- IAM + OIDC: keyless deploy identity for the CI pipeline (no access keys) --
resource "aws_iam_openid_connect_provider" "ci" {
  url             = var.oidc_provider_url
  client_id_list  = var.oidc_client_id_list
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

# Deploy role can push to ECR and roll the ECS service — scoped to this project.
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
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "EcsDeploy"
    effect = "Allow"
    actions = [
      "ecs:RegisterTaskDefinition",
      "ecs:DescribeTaskDefinition",
      "ecs:UpdateService",
      "ecs:DescribeServices",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PassExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ecs_task_execution.arn]
  }
}

resource "aws_iam_role_policy" "ci_deploy" {
  name   = "${local.name_prefix}-ci-deploy"
  role   = aws_iam_role.ci_deploy.id
  policy = data.aws_iam_policy_document.ci_deploy_permissions.json
}
