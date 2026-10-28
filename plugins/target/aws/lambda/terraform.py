"""Terraform (HCL) the aws-lambda plugin supplies as its backing IaC.

 — the aws-lambda ``IaC`` cell is **Lambda + IAM/OIDC (Terraform)**.
This module renders that infrastructure as valid HCL, parameterised by the IR and
by Terraform *variables* — never by literal account ids, secrets, or ARNs.

What the module provisions
--------------------------
* **Lambda function** — the deployable workload. The ``package`` verb produces an
 immutable artifact (an S3 object version for the zip path, or an ECR image digest
 for the optional container path) and the ``deploy`` verb points the function at
 it. The function source is a *variable* (``artifact_bucket`` / ``artifact_key``
 for zip, or ``image_uri`` for container) so no artifact value is committed.
* **S3 artifact bucket** — holds the versioned deployment zip the ``package`` verb
 uploads (the promotable, immutable reference is an S3 object version id). Versioning
 is enabled so each upload is addressable by an immutable version id.
* **IAM execution role** — the role Lambda assumes at run time (``lambda.amazonaws.com``
 trust) with the basic-execution managed policy for CloudWatch Logs.
* **IAM + OIDC** — a GitHub/Azure OIDC identity provider and a deploy role with a
 trust policy scoped to the client's repo, so the pipeline authenticates
 *keylessly* (no long-lived access keys — the R6/OIDC story). The deploy role can
 upload artifacts and update the function/alias — scoped, no wildcards on identity.

Why variables, not literals
----------------------------
The account id, the OIDC subject (``repo:org/name:*``), the artifact
bucket/key/image, and the handler/runtime differ per client and per run. Baking them
in would make the module non-portable and risk committing account-specific values.
They are therefore Terraform ``variable`` blocks the bootstrap step fills from the
local (git-ignored) config. The only values *derived from the IR* are the project
name (resource naming) and the region (provider + defaults), both non-secret.

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
    """Turn an IR project name into a safe, stable resource-name prefix.

 Lambda/IAM/S3 names allow ``[A-Za-z0-9-_]`` (S3 bucket names are stricter — lower
 case + dashes); project names are user-defined. We lower-case, replace any other
 run of characters with a single ``-``, and trim stray dashes so the prefix is
 deterministic and valid across all services.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/account-specific input (no literals).

 Keeping these as variables (not hard-coded values) is what makes the module
 portable and keeps account ids / OIDC subjects / artifact refs out of the
 committed files. The bootstrap step supplies them from the local
 git-ignored config.

 Args:
 region: the IR ``cloud.region`` — used only as the ``aws_region`` *default*
 so the IR drives the region without hard-coding it in a resource. A
 region is not a secret, so surfacing it as a default is safe.
 """
    region_default = region if region else "us-east-1"
    header = (
        "# Inputs for the aws-lambda backing infrastructure (generated — R6: no literal\n"
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

variable "lambda_runtime" {
  description = "Lambda managed runtime for the zip package (ignored for container images)."
  type        = string
  default     = "python3.12"
}

variable "lambda_handler" {
  description = "Function handler entrypoint (e.g. app.handler) for the zip package."
  type        = string
  default     = "app.handler"
}

variable "package_type" {
  description = "Lambda packaging type: Zip (S3 artifact) or Image (ECR container)."
  type        = string
  default     = "Zip"
}

variable "artifact_key" {
  description = "S3 object key of the initial deployment zip (Zip package_type)."
  type        = string
  default     = "lambda/bootstrap.zip"
}

variable "image_uri" {
  description = "Container image URI (repo@sha256:...) for Image package_type; empty for Zip."
  type        = string
  default     = ""
}

variable "memory_size" {
  description = "Function memory (MiB), which also scales CPU."
  type        = number
  default     = 256
}

variable "timeout_seconds" {
  description = "Function timeout in seconds."
  type        = number
  default     = 30
}

variable "alias_name" {
  description = "Lambda alias the deploy verb points at the new version (e.g. live)."
  type        = string
  default     = "live"
}

variable "enable_logging" {
  description = "Manage the function's CloudWatch log group via Terraform. Off by default: Lambda auto-creates it, and some org policies restrict the Logs management APIs."
  type        = bool
  default     = false
}

variable "lambda_handler_default" {
  description = "Default handler used only for the initial placeholder function; the deploy verb updates the real code."
  type        = string
  default     = "app.handler"
}

variable "use_oidc" {
  description = "Create the GitHub-OIDC provider + federated deploy role (keyless). Set false to use static access keys instead (e.g. where an org SCP denies creating an OIDC provider)."
  type        = bool
  default     = false
}

variable "oidc_provider_url" {
  description = "CI provider OIDC issuer URL (used only when use_oidc = true)."
  type        = string
  default     = "https://token.actions.githubusercontent.com"
}

variable "oidc_subject" {
  description = "OIDC subject the deploy role trusts (used only when use_oidc = true)."
  type        = string
  default     = ""
}

variable "oidc_client_id_list" {
  description = "Allowed OIDC audiences for the identity provider."
  type        = list(string)
  default     = ["sts.amazonaws.com"]
}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — Lambda + S3 artifact + IAM/OIDC for ``project_name``.

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
# aws-lambda backing infrastructure (generated — design §11.9: Lambda, IAM/OIDC).
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
    archive = {{
      source  = "hashicorp/archive"
      version = ">= 2.0"
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
  is_image    = var.package_type == "Image"
}}

# A tiny valid placeholder zip so the FIRST `terraform apply` can create the
# function before any real artifact exists in S3 (fixes first-run NoSuchKey). The
# CI pipeline's deploy verb replaces the code on every push; Terraform ignores
# that drift (see ignore_changes below). Deterministic, no network.
data "archive_file" "bootstrap" {{
  count       = local.is_image ? 0 : 1
  type        = "zip"
  output_path = "${{path.module}}/.bootstrap/placeholder.zip"

  source {{
    filename = "bootstrap.txt"
    content  = "placeholder — replaced by the CI deploy on first push"
  }}
}}

# --- S3: holds the versioned deployment zip the package verb uploads -----------
resource "aws_s3_bucket" "artifacts" {{
  bucket = "${{local.name_prefix}}-${{local.account_id}}-lambda-artifacts"
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

# --- CloudWatch log group for the function (optional) --------------------------
# Off by default: Lambda auto-creates its own log group on first invoke, and some
# org policies restrict the CloudWatch Logs management APIs. Enable to manage the
# group's retention explicitly via Terraform.
resource "aws_cloudwatch_log_group" "app" {{
  count             = var.enable_logging ? 1 : 0
  name              = "/aws/lambda/${{local.name_prefix}}"
  retention_in_days = 30
}}

# --- IAM: Lambda execution role (assumed by the service at run time) -----------
data "aws_iam_policy_document" "lambda_assume" {{
  statement {{
    actions = ["sts:AssumeRole"]
    principals {{
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }}
  }}
}}

resource "aws_iam_role" "lambda_exec" {{
  name               = "${{local.name_prefix}}-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}}

resource "aws_iam_role_policy_attachment" "lambda_basic" {{
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}}

# --- Lambda: the deployable workload (deploy verb target) ----------------------
# The source is a VARIABLE (S3 object for Zip, image uri for Image) so no artifact
# value is committed; the deploy verb publishes new versions and moves the alias.
resource "aws_lambda_function" "app" {{
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

  lifecycle {{
    # The deploy verb updates code/image out of band; ignore drift on those.
    ignore_changes = [filename, source_code_hash, s3_bucket, s3_key, image_uri]
  }}
}}

# NOTE: aws_lambda_function.depends_on tolerates an empty log-group list when
# logging is disabled (count = 0 yields an empty tuple, a valid dependency set).

resource "aws_lambda_alias" "live" {{
  name             = var.alias_name
  function_name    = aws_lambda_function.app.function_name
  function_version = aws_lambda_function.app.version
}}

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

# Deploy role can stage artifacts and roll the function — scoped to this project.
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
  }}

  statement {{
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
      "${{aws_lambda_function.app.arn}}:*",
    ]
  }}

  statement {{
    sid       = "PassExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.lambda_exec.arn]
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
    """Render ``outputs.tf`` — the handles the pipeline/bootstrap need downstream.

 These are the non-secret identifiers the verbs reference (function name, alias,
 artifact bucket, the deploy role ARN the CI OIDC step assumes). Outputs make the
 module composable without re-deriving names elsewhere.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "lambda_function_name" {
  description = "Lambda function name the deploy/verify verbs target."
  value       = aws_lambda_function.app.function_name
}

output "lambda_function_arn" {
  description = "Lambda function ARN (e.g. for a state machine to invoke)."
  value       = aws_lambda_function.app.arn
}

output "lambda_exec_role_name" {
  description = "Name of the function's execution role (for attaching extra policies)."
  value       = aws_iam_role.lambda_exec.name
}

output "lambda_alias_name" {
  description = "Lambda alias the deploy verb moves to the new version."
  value       = aws_lambda_alias.live.name
}

output "artifact_bucket" {
  description = "Versioned S3 bucket the package verb uploads the deployment zip to."
  value       = aws_s3_bucket.artifacts.id
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC (empty under key auth)."
  value       = length(aws_iam_role.ci_deploy) > 0 ? aws_iam_role.ci_deploy[0].arn : ""
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input.

 Mirrors the ``*.example`` convention: documents the keys with
 *placeholder* values only — never real account ids, artifact refs, or ARNs.
 """
    region_value = region if region else "us-east-1"
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the aws-lambda backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

aws_region   = "{region_value}"
project_name = "{prefix}"

# Packaging: "Zip" (S3 artifact) or "Image" (ECR container image).
package_type   = "Zip"
lambda_runtime = "python3.12"
lambda_handler = "app.handler"
artifact_key   = "lambda/bootstrap.zip"

# For package_type = "Image", set an immutable digest and leave the zip keys as-is:
# package_type = "Image"
# image_uri    = "ACCOUNT.dkr.ecr.REGION.amazonaws.com/REPO@sha256:REPLACE_ME"

alias_name = "live"

# CI OIDC trust — scope the subject to your repo/branch.
oidc_provider_url = "https://token.actions.githubusercontent.com"
oidc_subject      = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_client_id_list = ["{_DEFAULT_OIDC_AUDIENCE}"]
"""
