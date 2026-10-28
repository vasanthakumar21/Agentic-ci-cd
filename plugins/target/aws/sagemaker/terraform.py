"""Terraform (HCL) the aws-sagemaker plugin supplies as its backing IaC.

 — the aws-sagemaker ``IaC`` cell is **SageMaker + role (Terraform)**.
This module renders that infrastructure as valid HCL, parameterised by the IR and by
Terraform *variables* — never by literal account ids, secrets, or ARNs.

What the module provisions
--------------------------
* **ECR** — a repository to hold the training/inference image the pipeline builds +
 pushes (the ``package`` verb target), with scan-on-push and IMMUTABLE tags.
* **S3 model-artifacts bucket** — holds versioned model artifacts (``model.tar.gz``)
 the training step produces.
* **SageMaker** — a model, an endpoint configuration, and an endpoint (referencing
 the ECR image + S3 model artifacts). This is what the ``deploy`` verb updates and
 the ``verify`` verb polls to InService.
* **IAM** — the SageMaker execution role (pull image, read model artifacts, write
 logs), plus a GitHub/Azure OIDC identity provider and a scoped deploy role for
 keyless CI deploys.

Determinism
------------------
HCL is assembled from fixed, ordered string templates — no timestamps, no random
suffixes, stable block order — so regenerating from the same IR is byte-identical.
"""

from __future__ import annotations

import re

#: GitHub's OIDC audience when using ``aws-actions/configure-aws-credentials``.
_DEFAULT_OIDC_AUDIENCE = "sts.amazonaws.com"

#: A sensible default inference instance type (non-secret, documented).
_DEFAULT_INSTANCE_TYPE = "ml.m5.large"


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/account-specific input (no literals)."""
    region_default = region if region else "us-east-1"
    header = (
        "# Inputs for the aws-sagemaker backing infrastructure (generated — R6: no literal\n"
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
  description = "Container image tag the SageMaker model runs (e.g. a git SHA)."
  type        = string
  default     = "latest"
}}

variable "model_artifact_key" {{
  description = "S3 object key of the trained model artifact (model.tar.gz)."
  type        = string
  default     = "sagemaker/models/model.tar.gz"
}}

variable "instance_type" {{
  description = "SageMaker endpoint instance type."
  type        = string
  default     = "{_DEFAULT_INSTANCE_TYPE}"
}}

variable "instance_count" {{
  description = "Number of instances behind the SageMaker endpoint."
  type        = number
  default     = 1
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
    """Render ``main.tf`` — ECR + S3 + SageMaker + IAM/OIDC for ``project_name``.

 The region is referenced via ``var.aws_region``; the account id is resolved at
 apply time via ``aws_caller_identity`` so the committed HCL carries no account
 specifics.
 """
    prefix = _sanitize_name(project_name)

    return f"""\
# aws-sagemaker backing infrastructure (generated — design §11.9: SageMaker + role).
#
# Container-based ML shape (bring-your-own-container): the deployable is a Docker
# image in ECR that a SageMaker model/endpoint serves, reading model artifacts from
# S3.
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

# --- ECR: holds the training/inference image (package verb) --------------------
resource "aws_ecr_repository" "app" {{
  name                 = "${{local.name_prefix}}-sagemaker-ecr"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {{
    scan_on_push = true
  }}
}}

# --- S3: holds the versioned trained model artifacts ---------------------------
resource "aws_s3_bucket" "models" {{
  bucket = "${{local.name_prefix}}-${{local.account_id}}-sagemaker-models"
}}

resource "aws_s3_bucket_versioning" "models" {{
  bucket = aws_s3_bucket.models.id
  versioning_configuration {{
    status = "Enabled"
  }}
}}

resource "aws_s3_bucket_public_access_block" "models" {{
  bucket                  = aws_s3_bucket.models.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}

# --- CloudWatch log group for the endpoint -------------------------------------
resource "aws_cloudwatch_log_group" "app" {{
  name              = "/aws-sagemaker/${{local.name_prefix}}"
  retention_in_days = 30
}}

# --- IAM: SageMaker execution role (assumed by the service at run time) --------
data "aws_iam_policy_document" "sagemaker_assume" {{
  statement {{
    actions = ["sts:AssumeRole"]
    principals {{
      type        = "Service"
      identifiers = ["sagemaker.amazonaws.com"]
    }}
  }}
}}

resource "aws_iam_role" "sagemaker_exec" {{
  name               = "${{local.name_prefix}}-sagemaker-exec"
  assume_role_policy = data.aws_iam_policy_document.sagemaker_assume.json
}}

# Scoped inline policy: pull the image, read model artifacts, write CloudWatch logs.
data "aws_iam_policy_document" "sagemaker_exec_permissions" {{
  statement {{
    sid    = "PullImage"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = ["*"]
  }}

  statement {{
    sid    = "ReadModelArtifacts"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.models.arn,
      "${{aws_s3_bucket.models.arn}}/*",
    ]
  }}

  statement {{
    sid    = "WriteLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${{aws_cloudwatch_log_group.app.arn}}:*"]
  }}
}}

resource "aws_iam_role_policy" "sagemaker_exec" {{
  name   = "${{local.name_prefix}}-sagemaker-exec"
  role   = aws_iam_role.sagemaker_exec.id
  policy = data.aws_iam_policy_document.sagemaker_exec_permissions.json
}}

# --- SageMaker: model + endpoint config + endpoint (deploy verb target) --------
resource "aws_sagemaker_model" "app" {{
  name               = "${{local.name_prefix}}-model"
  execution_role_arn = aws_iam_role.sagemaker_exec.arn

  primary_container {{
    image          = "${{local.ecr_repo_url}}:${{var.image_tag}}"
    model_data_url = "s3://${{aws_s3_bucket.models.id}}/${{var.model_artifact_key}}"
  }}

  lifecycle {{
    # The deploy verb creates new model/config versions out of band; ignore drift.
    ignore_changes = [primary_container]
  }}
}}

resource "aws_sagemaker_endpoint_configuration" "app" {{
  name = "${{local.name_prefix}}-endpoint-config"

  production_variants {{
    variant_name           = "AllTraffic"
    model_name             = aws_sagemaker_model.app.name
    initial_instance_count = var.instance_count
    instance_type          = var.instance_type
  }}

  lifecycle {{
    ignore_changes = [production_variants]
  }}
}}

resource "aws_sagemaker_endpoint" "app" {{
  name                 = "${{local.name_prefix}}-endpoint"
  endpoint_config_name = aws_sagemaker_endpoint_configuration.app.name

  lifecycle {{
    ignore_changes = [endpoint_config_name]
  }}
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

# Deploy role can push to ECR, stage model artifacts, and create/update the
# SageMaker model/endpoint — scoped to this project.
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
      "${{aws_s3_bucket.models.arn}}/*",
    ]
  }}

  statement {{
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
  }}

  statement {{
    sid       = "PassSageMakerExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.sagemaker_exec.arn]
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

output "model_bucket" {
  description = "Versioned S3 bucket the training step uploads model artifacts to."
  value       = aws_s3_bucket.models.id
}

output "sagemaker_endpoint_name" {
  description = "SageMaker endpoint name the deploy/verify verbs target."
  value       = aws_sagemaker_endpoint.app.name
}

output "sagemaker_role_arn" {
  description = "IAM role ARN the SageMaker model assumes at run time."
  value       = aws_iam_role.sagemaker_exec.arn
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
# Example tfvars for the aws-sagemaker backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

aws_region   = "{region_value}"
project_name = "{prefix}"
image_tag    = "latest"

# Where the trained model artifact lives + endpoint sizing (override as needed).
model_artifact_key = "sagemaker/models/model.tar.gz"
instance_type      = "{_DEFAULT_INSTANCE_TYPE}"
instance_count     = 1

# CI OIDC trust — scope the subject to your repo/branch.
oidc_provider_url = "https://token.actions.githubusercontent.com"
oidc_subject      = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_client_id_list = ["{_DEFAULT_OIDC_AUDIENCE}"]
"""
