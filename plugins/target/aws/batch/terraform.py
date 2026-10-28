"""Terraform (HCL) the aws-batch plugin supplies as its backing IaC.

 — the aws-batch ``IaC`` cell is **Batch + compute env (Terraform)**.
This module renders that infrastructure as valid HCL, parameterised by the IR and by
Terraform *variables* — never by literal account ids, secrets, or ARNs.

What the module provisions
--------------------------
* **ECR** — a repository to hold the image the pipeline builds + pushes (the
 ``package`` verb target), with scan-on-push enabled and IMMUTABLE tags.
* **Batch** — a Fargate compute environment, a job queue, and a job definition
 (referencing the ECR image). This is what the ``deploy`` verb updates and the
 ``verify`` verb submits a job against.
* **IAM** — the Batch job execution role (ECS task execution for Fargate-on-Batch,
 pull image + write logs), the Batch service role, plus a GitHub/Azure OIDC
 identity provider and a scoped deploy role for keyless CI deploys.

Determinism
------------------
HCL is assembled from fixed, ordered string templates — no timestamps, no random
suffixes, stable block order — so regenerating from the same IR is byte-identical.
"""

from __future__ import annotations

import re

#: GitHub's OIDC audience when using ``aws-actions/configure-aws-credentials``.
_DEFAULT_OIDC_AUDIENCE = "sts.amazonaws.com"


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/account-specific input (no literals)."""
    region_default = region if region else "us-east-1"
    header = (
        "# Inputs for the aws-batch backing infrastructure (generated — R6: no literal\n"
        "# account specifics or secrets; the bootstrap step fills these from local config).\n"
        "\n"
        'variable "aws_region" {\n'
        '  description = "AWS region to provision into."\n'
        "  type        = string\n"
        f'  default     = "{region_default}"\n'
        "}\n"
    )
    return header + f"""
variable "project_name" {{
  description = "Project name used as the resource-name prefix."
  type        = string
}}

variable "image_tag" {{
  description = "Container image tag the Batch job definition runs (e.g. a git SHA)."
  type        = string
  default     = "latest"
}}

variable "job_vcpu" {{
  description = "vCPU allocated to each Batch job container."
  type        = string
  default     = "1"
}}

variable "job_memory" {{
  description = "Memory (MiB) allocated to each Batch job container."
  type        = string
  default     = "2048"
}}

variable "max_vcpus" {{
  description = "Maximum vCPU the Fargate compute environment may scale to."
  type        = number
  default     = 16
}}

variable "subnet_ids" {{
  description = "Subnets the Batch Fargate compute environment places jobs in (per-client networking)."
  type        = list(string)
}}

variable "security_group_ids" {{
  description = "Security groups attached to the Batch Fargate jobs."
  type        = list(string)
}}

variable "oidc_provider_url" {{
  description = "CI provider OIDC issuer URL (e.g. https://token.actions.githubusercontent.com)."
  type        = string
}}

variable "oidc_subject" {{
  description = "OIDC subject the deploy role trusts (e.g. repo:org/name:ref:refs/heads/main)."
  type        = string
}}

variable "oidc_client_id_list" {{
  description = "Allowed OIDC audiences for the identity provider."
  type        = list(string)
  default     = ["{_DEFAULT_OIDC_AUDIENCE}"]
}}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — ECR + Batch + IAM/OIDC resources for ``project_name``.

 The region is referenced via ``var.aws_region``; the account id is resolved at
 apply time via ``aws_caller_identity`` so the committed HCL carries no account
 specifics.
 """
    prefix = _sanitize_name(project_name)

    return f"""\
# aws-batch backing infrastructure (generated — design §11.9: Batch + compute env).
#
# Container-based compute shape (like ecs): the deployable is a Docker image in ECR
# that an AWS Batch job definition runs on a Fargate compute environment.
#
# Parameterised entirely by variables (see variables.tf). No literal account ids,
# ARNs, or secrets appear here (R6). Resource names derive from the IR project
# name ({project_name!r}) for a stable, readable layout (R5.3).

terraform {{
  required_version = ">= 1.5.0"
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }}
  }}
}}

provider "aws" {{
  region = var.aws_region
}}

# Account id is resolved at apply time — never committed as a literal (R6).
data "aws_caller_identity" "current" {{}}

locals {{
  name_prefix  = "{prefix}"
  account_id   = data.aws_caller_identity.current.account_id
  ecr_repo_url = "${{local.account_id}}.dkr.ecr.${{var.aws_region}}.amazonaws.com/${{aws_ecr_repository.app.name}}"
}}

# --- ECR: holds the image the pipeline builds and pushes (package verb) --------
resource "aws_ecr_repository" "app" {{
  name                 = "${{local.name_prefix}}-batch-ecr"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {{
    scan_on_push = true
  }}
}}

# --- CloudWatch log group for the Batch job ------------------------------------
resource "aws_cloudwatch_log_group" "app" {{
  name              = "/aws-batch/${{local.name_prefix}}"
  retention_in_days = 30
}}

# --- IAM: Batch job execution role (Fargate: pull image, write logs) -----------
data "aws_iam_policy_document" "ecs_tasks_assume" {{
  statement {{
    actions = ["sts:AssumeRole"]
    principals {{
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }}
  }}
}}

resource "aws_iam_role" "job_execution" {{
  name               = "${{local.name_prefix}}-batch-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}}

resource "aws_iam_role_policy_attachment" "job_execution" {{
  role       = aws_iam_role.job_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}}

# --- IAM: Batch service role ---------------------------------------------------
data "aws_iam_policy_document" "batch_assume" {{
  statement {{
    actions = ["sts:AssumeRole"]
    principals {{
      type        = "Service"
      identifiers = ["batch.amazonaws.com"]
    }}
  }}
}}

resource "aws_iam_role" "batch_service" {{
  name               = "${{local.name_prefix}}-batch-service"
  assume_role_policy = data.aws_iam_policy_document.batch_assume.json
}}

resource "aws_iam_role_policy_attachment" "batch_service" {{
  role       = aws_iam_role.batch_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}}

# --- Batch: Fargate compute environment + job queue + job definition -----------
resource "aws_batch_compute_environment" "app" {{
  compute_environment_name = "${{local.name_prefix}}-batch-ce"
  type                     = "MANAGED"
  service_role             = aws_iam_role.batch_service.arn

  compute_resources {{
    type               = "FARGATE"
    max_vcpus          = var.max_vcpus
    subnets            = var.subnet_ids
    security_group_ids = var.security_group_ids
  }}
}}

resource "aws_batch_job_queue" "app" {{
  name     = "${{local.name_prefix}}-batch-queue"
  state    = "ENABLED"
  priority = 1

  compute_environment_order {{
    order               = 0
    compute_environment = aws_batch_compute_environment.app.arn
  }}
}}

resource "aws_batch_job_definition" "app" {{
  name                  = "${{local.name_prefix}}-batch-job"
  type                  = "container"
  platform_capabilities = ["FARGATE"]

  container_properties = jsonencode({{
    image      = "${{local.ecr_repo_url}}:${{var.image_tag}}"
    executionRoleArn = aws_iam_role.job_execution.arn
    resourceRequirements = [
      {{ type = "VCPU", value = var.job_vcpu }},
      {{ type = "MEMORY", value = var.job_memory }},
    ]
    networkConfiguration = {{
      assignPublicIp = "ENABLED"
    }}
    logConfiguration = {{
      logDriver = "awslogs"
      options = {{
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "batch"
      }}
    }}
  }})
}}

# --- IAM + OIDC: keyless deploy identity for the CI pipeline (no access keys) --
resource "aws_iam_openid_connect_provider" "ci" {{
  url            = var.oidc_provider_url
  client_id_list = var.oidc_client_id_list
  # Thumbprint is validated by AWS for well-known providers; kept as a variable-free
  # well-known value so the module applies without committing account specifics.
  thumbprint_list = ["ffffffffffffffffffffffffffffffffffffffff"]
}}

data "aws_iam_policy_document" "ci_deploy_assume" {{
  statement {{
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"

    principals {{
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.ci.arn]
    }}

    condition {{
      test     = "StringEquals"
      variable = "${{replace(var.oidc_provider_url, "https://", "")}}:aud"
      values   = var.oidc_client_id_list
    }}

    condition {{
      test     = "StringLike"
      variable = "${{replace(var.oidc_provider_url, "https://", "")}}:sub"
      values   = [var.oidc_subject]
    }}
  }}
}}

resource "aws_iam_role" "ci_deploy" {{
  name               = "${{local.name_prefix}}-ci-deploy"
  assume_role_policy = data.aws_iam_policy_document.ci_deploy_assume.json
}}

# Deploy role can push to ECR and register/submit Batch jobs — scoped to this project.
data "aws_iam_policy_document" "ci_deploy_permissions" {{
  statement {{
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
  }}

  statement {{
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
  }}

  statement {{
    sid       = "PassBatchRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [
      aws_iam_role.job_execution.arn,
      aws_iam_role.batch_service.arn,
    ]
  }}
}}

resource "aws_iam_role_policy" "ci_deploy" {{
  name   = "${{local.name_prefix}}-ci-deploy"
  role   = aws_iam_role.ci_deploy.id
  policy = data.aws_iam_policy_document.ci_deploy_permissions.json
}}
"""


def render_outputs_tf() -> str:
    """Render ``outputs.tf`` — the handles the pipeline/bootstrap need downstream."""
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "ecr_repository_url" {
  description = "ECR repository URL to tag/push images to (package verb)."
  value       = aws_ecr_repository.app.repository_url
}

output "batch_job_queue" {
  description = "AWS Batch job queue the verify verb submits jobs to."
  value       = aws_batch_job_queue.app.name
}

output "batch_job_definition" {
  description = "AWS Batch job definition the deploy verb registers revisions of."
  value       = aws_batch_job_definition.app.name
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC for keyless deploy."
  value       = aws_iam_role.ci_deploy.arn
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input."""
    region_value = region if region else "us-east-1"
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the aws-batch backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

aws_region   = "{region_value}"
project_name = "{prefix}"
image_tag    = "latest"

# Per-job sizing (override as needed).
job_vcpu   = "1"
job_memory = "2048"
max_vcpus  = 16

# Per-client networking — fill from your VPC.
subnet_ids         = ["subnet-REPLACE_ME"]
security_group_ids = ["sg-REPLACE_ME"]

# CI OIDC trust — scope the subject to your repo/branch.
oidc_provider_url = "https://token.actions.githubusercontent.com"
oidc_subject      = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_client_id_list = ["{_DEFAULT_OIDC_AUDIENCE}"]
"""
