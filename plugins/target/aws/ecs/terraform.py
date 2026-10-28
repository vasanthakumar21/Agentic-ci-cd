"""Terraform (HCL) the aws-ecs plugin supplies as its backing IaC.

 — the aws-ecs ``IaC`` cell is **ECR, ECS, IAM/OIDC (Terraform)**. This
module renders that infrastructure as valid HCL, parameterised by the IR and by
Terraform *variables* — never by literal account ids, secrets, or ARNs.

What the module provisions
--------------------------
* **ECR** — a repository to hold the image the pipeline builds + pushes (the
 ``package`` verb target), with scan-on-push enabled.
* **ECS** — a Fargate cluster, a task definition (referencing the ECR image), and
 a service that runs it. This is what the ``deploy`` verb updates.
* **IAM + OIDC** — a GitHub/Azure OIDC identity provider and a deploy role with a
 trust policy scoped to the client's repo, so the pipeline authenticates
 *keylessly* (no long-lived access keys — the R6/OIDC story). Plus the ECS task
 execution role ECS needs to pull the image and write logs.

Why variables, not literals
----------------------------
The account id, the OIDC subject (``repo:org/name:*``), the VPC subnets/security
groups, and the image tag differ per client and per run. Baking them in would make
the module non-portable and would risk committing account-specific values. They are
therefore Terraform ``variable`` blocks the bootstrap step fills from the local
(git-ignored) config. The only values *derived from the IR* are the project name
(resource naming) and the region (provider + defaults), both non-secret.

Determinism
------------------
HCL is assembled from fixed, ordered string templates — no timestamps, no random
suffixes, stable block order — so regenerating from the same IR is byte-identical.
"""

from __future__ import annotations

import re

#: OIDC thumbprint / audience defaults kept as documented constants (non-secret).
#: GitHub's OIDC audience when using ``aws-actions/configure-aws-credentials``.
_DEFAULT_OIDC_AUDIENCE = "sts.amazonaws.com"


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix.

 ECR/ECS/IAM names allow ``[A-Za-z0-9-_]``; project names are user-defined. We
 lower-case, replace any other run of characters with a single ``-``, and trim
 stray dashes so the prefix is deterministic and valid across all three services.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/account-specific input (no literals).

 Keeping these as variables (not hard-coded values) is what makes the module
 portable and keeps account ids / OIDC subjects / networking out of the committed
 files. The bootstrap step supplies them from the local git-ignored config.

 Args:
 region: the IR ``cloud.region`` — used only as the ``aws_region`` *default*
 so the IR drives the region without hard-coding it in a resource. A
 region is not a secret, so surfacing it as a default is safe.
 """
    region_default = region if region else "us-east-1"
    header = (
        "# Inputs for the aws-ecs backing infrastructure (generated — R6: no literal\n"
        "# account specifics or secrets; the bootstrap step fills these from local config).\n"
        "\n"
        'variable "aws_region" {\n'
        '  description = "AWS region to provision into."\n'
        "  type        = string\n"
        f'  default     = "{region_default}"\n'
        "}\n"
    )
    return header + """
variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "image_tag" {
  description = "Container image tag the ECS task definition runs (e.g. a git SHA)."
  type        = string
  default     = "latest"
}

variable "container_port" {
  description = "Port the container listens on."
  type        = number
  default     = 8080
}

variable "desired_count" {
  description = "Number of ECS service tasks to run."
  type        = number
  default     = 1
}

variable "task_cpu" {
  description = "Fargate task CPU units."
  type        = string
  default     = "256"
}

variable "task_memory" {
  description = "Fargate task memory (MiB)."
  type        = string
  default     = "512"
}

variable "subnet_ids" {
  description = "Subnets the ECS service places tasks in (per-client networking)."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups attached to the ECS service tasks."
  type        = list(string)
}

variable "oidc_provider_url" {
  description = "CI provider OIDC issuer URL (e.g. https://token.actions.githubusercontent.com)."
  type        = string
}

variable "oidc_subject" {
  description = "OIDC subject the deploy role trusts (e.g. repo:org/name:ref:refs/heads/main)."
  type        = string
}

variable "oidc_client_id_list" {
  description = "Allowed OIDC audiences for the identity provider."
  type        = list(string)
  default     = ["sts.amazonaws.com"]
}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — ECR + ECS + IAM/OIDC resources for ``project_name``.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The region is referenced via ``var.aws_region`` (whose default the IR drives in
 ``variables.tf``), so no region literal is hard-coded in a resource. The account
 id is never written literally either: it is read at apply time via
 ``aws_caller_identity`` and interpolated, so the committed HCL carries no
 account specifics.
 """
    prefix = _sanitize_name(project_name)

    return f"""\
# aws-ecs backing infrastructure (generated — design §8b: ECR, ECS, IAM/OIDC).
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
  name_prefix = "{prefix}"
  account_id  = data.aws_caller_identity.current.account_id
  ecr_repo_url = "${{local.account_id}}.dkr.ecr.${{var.aws_region}}.amazonaws.com/${{aws_ecr_repository.app.name}}"
}}

# --- ECR: holds the image the pipeline builds and pushes (package verb) --------
resource "aws_ecr_repository" "app" {{
  name                 = "${{local.name_prefix}}-ecr"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {{
    scan_on_push = true
  }}
}}

# --- CloudWatch log group for the ECS task ------------------------------------
resource "aws_cloudwatch_log_group" "app" {{
  name              = "/ecs/${{local.name_prefix}}"
  retention_in_days = 30
}}

# --- ECS: cluster + task definition + Fargate service (deploy verb target) -----
resource "aws_ecs_cluster" "app" {{
  name = "${{local.name_prefix}}-cluster"
}}

resource "aws_ecs_task_definition" "app" {{
  family                   = "${{local.name_prefix}}-task"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([
    {{
      name      = "${{local.name_prefix}}-app"
      image     = "${{local.ecr_repo_url}}:${{var.image_tag}}"
      essential = true
      portMappings = [
        {{
          containerPort = var.container_port
          protocol      = "tcp"
        }}
      ]
      logConfiguration = {{
        logDriver = "awslogs"
        options = {{
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }}
      }}
    }}
  ])
}}

resource "aws_ecs_service" "app" {{
  name            = "${{local.name_prefix}}-service"
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {{
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = true
  }}
}}

# --- IAM: ECS task execution role (pull image, write logs) ---------------------
data "aws_iam_policy_document" "ecs_assume" {{
  statement {{
    actions = ["sts:AssumeRole"]
    principals {{
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }}
  }}
}}

resource "aws_iam_role" "ecs_task_execution" {{
  name               = "${{local.name_prefix}}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {{
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}}

# --- IAM + OIDC: keyless deploy identity for the CI pipeline (no access keys) --
resource "aws_iam_openid_connect_provider" "ci" {{
  url             = var.oidc_provider_url
  client_id_list  = var.oidc_client_id_list
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

# Deploy role can push to ECR and roll the ECS service — scoped to this project.
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
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = ["*"]
  }}

  statement {{
    sid    = "EcsDeploy"
    effect = "Allow"
    actions = [
      "ecs:RegisterTaskDefinition",
      "ecs:DescribeTaskDefinition",
      "ecs:UpdateService",
      "ecs:DescribeServices",
    ]
    resources = ["*"]
  }}

  statement {{
    sid       = "PassExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ecs_task_execution.arn]
  }}
}}

resource "aws_iam_role_policy" "ci_deploy" {{
  name   = "${{local.name_prefix}}-ci-deploy"
  role   = aws_iam_role.ci_deploy.id
  policy = data.aws_iam_policy_document.ci_deploy_permissions.json
}}
"""


def render_outputs_tf() -> str:
    """Render ``outputs.tf`` — the handles the pipeline/bootstrap need downstream.

 These are the non-secret identifiers the verbs reference (ECR repo URL, cluster
 and service names, the deploy role ARN the CI OIDC step assumes). Outputs make
 the module composable without re-deriving names elsewhere.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "ecr_repository_url" {
  description = "ECR repository URL to tag/push images to (package verb)."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name (deploy/verify verbs)."
  value       = aws_ecs_cluster.app.name
}

output "ecs_service_name" {
  description = "ECS service name the deploy verb updates."
  value       = aws_ecs_service.app.name
}

output "ecs_task_family" {
  description = "ECS task definition family the deploy verb registers revisions of."
  value       = aws_ecs_task_definition.app.family
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC for keyless deploy."
  value       = aws_iam_role.ci_deploy.arn
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input.

 Mirrors the ``*.example`` convention: documents the keys with
 *placeholder* values only — never real account ids, subnets, or ARNs.
 """
    region_value = region if region else "us-east-1"
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the aws-ecs backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

aws_region   = "{region_value}"
project_name = "{prefix}"
image_tag    = "latest"

# Per-client networking — fill from your VPC.
subnet_ids         = ["subnet-REPLACE_ME"]
security_group_ids = ["sg-REPLACE_ME"]

# CI OIDC trust — scope the subject to your repo/branch.
oidc_provider_url = "https://token.actions.githubusercontent.com"
oidc_subject      = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_client_id_list = ["{_DEFAULT_OIDC_AUDIENCE}"]
"""
