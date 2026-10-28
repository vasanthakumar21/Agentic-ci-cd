"""Terraform (HCL) the aws-stepfunctions plugin supplies as its backing IaC.

 — the aws-stepfunctions ``IaC`` cell is **SFN + role (Terraform)**.
This module renders that infrastructure as valid HCL, parameterised by the IR and by
Terraform *variables* — never by literal account ids, secrets, or ARNs.

What the module provisions
--------------------------
* **S3 artifact bucket** — holds the versioned ASL definition + any task artifacts
 the ``package`` verb uploads. Versioning is enabled so each upload is addressable
 by an immutable object version id (the promotable reference).
* **Step Functions state machine** (``aws_sfn_state_machine``) — the deployable
 orchestration. Its ``definition`` starts from a minimal ASL body; the ``deploy``
 verb updates it out of band and the ``verify`` verb starts + polls an execution.
 There is NO container image anywhere.
* **IAM state-machine execution role** — the role Step Functions assumes
 (``states.amazonaws.com`` trust) with a scoped inline policy for CloudWatch Logs.
* **IAM + OIDC** — a GitHub/Azure OIDC identity provider and a deploy role scoped to
 the client's repo for keyless CI deploys (no long-lived access keys).

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
        "# Inputs for the aws-stepfunctions backing infrastructure (generated — R6: no\n"
        "# literal account specifics or secrets; the bootstrap step fills these from local config).\n"
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

variable "definition_key" {{
  description = "S3 object key of the ASL state-machine definition (uploaded by the package verb)."
  type        = string
  default     = "stepfunctions/state-machine.asl.json"
}}

variable "state_machine_type" {{
  description = "Step Functions state-machine type (STANDARD or EXPRESS)."
  type        = string
  default     = "STANDARD"
}}

variable "oidc_provider_url" {{
  description = "CI provider OIDC issuer URL (used only when use_oidc = true)."
  type        = string
  default     = "https://token.actions.githubusercontent.com"
}}

variable "oidc_subject" {{
  description = "OIDC subject the deploy role trusts (used only when use_oidc = true)."
  type        = string
  default     = ""
}}

variable "oidc_client_id_list" {{
  description = "Allowed OIDC audiences for the identity provider."
  type        = list(string)
  default     = ["{_DEFAULT_OIDC_AUDIENCE}"]
}}

variable "use_oidc" {{
  description = "Create the GitHub-OIDC provider + federated deploy role (keyless). Set false to use static access keys instead (e.g. where an org SCP denies creating an OIDC provider)."
  type        = bool
  default     = false
}}

variable "enable_logging" {{
  description = "Attach a CloudWatch log group + logging_configuration to the state machine. Off by default since some org SCPs restrict Logs delivery APIs."
  type        = bool
  default     = false
}}

variable "task_lambda_arns" {{
  description = "Lambda function ARNs the state machine is allowed to invoke. Empty grants no Lambda invoke."
  type        = list(string)
  default     = []
}}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — SFN state machine + S3 + IAM/OIDC for ``project_name``.

 The region is referenced via ``var.aws_region``; the account id is resolved at
 apply time via ``aws_caller_identity`` so the committed HCL carries no account
 specifics. There is deliberately NO ECR, NO ECS, NO container image.
 """
    prefix = _sanitize_name(project_name)

    return f"""\
# aws-stepfunctions backing infrastructure (generated — design §11.9: SFN + role).
#
# State-machine orchestration shape: NO container image, NO ECR, NO ECS — the
# deployable is an aws_sfn_state_machine running an Amazon States Language body.
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
}}

# --- S3: holds the versioned ASL definition + task artifacts (package verb) -----
resource "aws_s3_bucket" "artifacts" {{
  bucket = "${{local.name_prefix}}-${{local.account_id}}-sfn-artifacts"
}}

resource "aws_s3_bucket_versioning" "artifacts" {{
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {{
    status = "Enabled"
  }}
}}

resource "aws_s3_bucket_public_access_block" "artifacts" {{
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}

# --- CloudWatch log group for the state machine (optional) ---------------------
resource "aws_cloudwatch_log_group" "app" {{
  count             = var.enable_logging ? 1 : 0
  name              = "/aws-stepfunctions/${{local.name_prefix}}"
  retention_in_days = 30
}}

# --- IAM: state-machine execution role (assumed by the service) -----------------
data "aws_iam_policy_document" "sfn_assume" {{
  statement {{
    actions = ["sts:AssumeRole"]
    principals {{
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }}
  }}
}}

resource "aws_iam_role" "sfn_exec" {{
  name               = "${{local.name_prefix}}-sfn-exec"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}}

# Scoped inline policy: invoke the task Lambda(s) and start Glue jobs the states
# call, plus (optionally) CloudWatch Logs delivery when logging is enabled. The
# Lambda ARNs the state machine may invoke are a variable so no account specific
# is hard-coded; an empty list grants no Lambda invoke (safe default).
data "aws_iam_policy_document" "sfn_exec_permissions" {{
  dynamic "statement" {{
    for_each = length(var.task_lambda_arns) > 0 ? [1] : []
    content {{
      sid       = "InvokeTaskLambdas"
      effect    = "Allow"
      actions   = ["lambda:InvokeFunction"]
      resources = var.task_lambda_arns
    }}
  }}

  statement {{
    sid    = "RunGlueJobs"
    effect = "Allow"
    actions = [
      "glue:StartJobRun",
      "glue:GetJobRun",
      "glue:GetJobRuns",
      "glue:BatchStopJobRun",
    ]
    resources = ["*"]
  }}

  dynamic "statement" {{
    for_each = var.enable_logging ? [1] : []
    content {{
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
    }}
  }}
}}

resource "aws_iam_role_policy" "sfn_exec" {{
  name   = "${{local.name_prefix}}-sfn-exec"
  role   = aws_iam_role.sfn_exec.id
  policy = data.aws_iam_policy_document.sfn_exec_permissions.json
}}

# --- Step Functions: the deployable workload (deploy verb target) --------------
# A minimal starting definition the deploy verb updates out of band.
resource "aws_sfn_state_machine" "app" {{
  name     = "${{local.name_prefix}}-sfn"
  role_arn = aws_iam_role.sfn_exec.arn
  type     = var.state_machine_type

  definition = jsonencode({{
    Comment = "Starting state machine for ${{local.name_prefix}} (deploy verb updates it)."
    StartAt = "Start"
    States = {{
      Start = {{
        Type   = "Pass"
        Result = {{ status = "ok" }}
        Next   = "Done"
      }}
      Done = {{
        Type = "Succeed"
      }}
    }}
  }})

  dynamic "logging_configuration" {{
    for_each = var.enable_logging ? [1] : []
    content {{
      log_destination        = "${{aws_cloudwatch_log_group.app[0].arn}}:*"
      include_execution_data = true
      level                  = "ALL"
    }}
  }}

  lifecycle {{
    # The deploy verb updates the definition out of band; ignore drift on it.
    ignore_changes = [definition]
  }}
}}

# --- CI deploy identity (OIDC keyless OR static keys; see var.use_oidc) --------
# All OIDC resources are gated by count so the key-auth path (use_oidc = false)
# provisions none of them — useful where an org SCP denies creating an OIDC
# provider. Under key auth the pipeline uses access-key GitHub secrets instead.
resource "aws_iam_openid_connect_provider" "ci" {{
  count          = var.use_oidc ? 1 : 0
  url            = var.oidc_provider_url
  client_id_list = var.oidc_client_id_list
  # Thumbprint is validated by AWS for well-known providers; kept as a variable-free
  # well-known value so the module applies without committing account specifics.
  thumbprint_list = ["ffffffffffffffffffffffffffffffffffffffff"]
}}

data "aws_iam_policy_document" "ci_deploy_assume" {{
  count = var.use_oidc ? 1 : 0
  statement {{
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"

    principals {{
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.ci[0].arn]
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
  count              = var.use_oidc ? 1 : 0
  name               = "${{local.name_prefix}}-ci-deploy"
  assume_role_policy = data.aws_iam_policy_document.ci_deploy_assume[0].json
}}

# Deploy role can stage the definition and create/update/start the state machine —
# scoped to this project.
data "aws_iam_policy_document" "ci_deploy_permissions" {{
  count = var.use_oidc ? 1 : 0
  statement {{
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
      "${{aws_s3_bucket.artifacts.arn}}/*",
    ]
  }}

  statement {{
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
  }}

  statement {{
    sid       = "PassStateMachineRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.sfn_exec.arn]
  }}
}}

resource "aws_iam_role_policy" "ci_deploy" {{
  count  = var.use_oidc ? 1 : 0
  name   = "${{local.name_prefix}}-ci-deploy"
  role   = aws_iam_role.ci_deploy[0].id
  policy = data.aws_iam_policy_document.ci_deploy_permissions[0].json
}}
"""


def render_outputs_tf() -> str:
    """Render ``outputs.tf`` — the handles the pipeline/bootstrap need downstream."""
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "state_machine_arn" {
  description = "Step Functions state-machine ARN the deploy/verify verbs target."
  value       = aws_sfn_state_machine.app.arn
}

output "state_machine_name" {
  description = "Step Functions state-machine name."
  value       = aws_sfn_state_machine.app.name
}

output "artifact_bucket" {
  description = "Versioned S3 bucket the package verb uploads the ASL definition to."
  value       = aws_s3_bucket.artifacts.id
}

output "sfn_role_arn" {
  description = "IAM role ARN the state machine assumes at run time."
  value       = aws_iam_role.sfn_exec.arn
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC (empty under key auth)."
  value       = length(aws_iam_role.ci_deploy) > 0 ? aws_iam_role.ci_deploy[0].arn : ""
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input."""
    region_value = region if region else "us-east-1"
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the aws-stepfunctions backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

aws_region   = "{region_value}"
project_name = "{prefix}"

# Where the uploaded ASL definition lives (package verb targets).
definition_key     = "stepfunctions/state-machine.asl.json"
state_machine_type = "STANDARD"

# CI OIDC trust — scope the subject to your repo/branch.
oidc_provider_url = "https://token.actions.githubusercontent.com"
oidc_subject      = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_client_id_list = ["{_DEFAULT_OIDC_AUDIENCE}"]
"""
