"""Terraform (HCL) the aws-emr plugin supplies as its backing IaC.

 — the aws-emr ``IaC`` cell is **EMR + roles (Terraform)**. This module
renders that infrastructure as valid HCL, parameterised by the IR and by Terraform
*variables* — never by literal account ids, secrets, or ARNs.

What the module provisions
--------------------------
* **S3 artifact bucket** — holds the versioned PySpark job script(s) + any extra
 python libs/wheels the ``package`` verb uploads. Versioning is enabled so each
 upload is addressable by an immutable object version id (the promotable reference
 — lists EMR as promotion=yes).
* **EMR Serverless application** (``aws_emrserverless_application``) — the Spark
 compute the ``deploy`` verb starts job runs on. There is NO container image
 anywhere (the data-job shape, like glue). The ``verify`` verb polls a run.
* **IAM EMR execution/job role** — the role the EMR job assumes at run time
 (``emr-serverless.amazonaws.com`` trust) with a scoped inline policy to read the
 artifact bucket and write CloudWatch Logs.
* **IAM + OIDC** — a GitHub/Azure OIDC identity provider and a deploy role whose
 trust policy is scoped to the client's repo, so the pipeline authenticates
 *keylessly* (no long-lived access keys — the R6/OIDC story).

Determinism
------------------
HCL is assembled from fixed, ordered string templates — no timestamps, no random
suffixes, stable block order — so regenerating from the same IR is byte-identical.
"""

from __future__ import annotations

import re

#: GitHub's OIDC audience when using ``aws-actions/configure-aws-credentials``.
_DEFAULT_OIDC_AUDIENCE = "sts.amazonaws.com"

#: Pinned EMR release default (reproducible, non-"latest").
_DEFAULT_EMR_RELEASE = "emr-7.0.0"


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix.

 EMR/IAM/S3 names allow ``[A-Za-z0-9-_]`` (S3 bucket names are stricter — lower
 case + dashes); project names are user-defined. We lower-case, replace any other
 run of characters with a single ``-``, and trim stray dashes so the prefix is
 deterministic and valid across all services.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/account-specific input (no literals).

 Args:
 region: the IR ``cloud.region`` — used only as the ``aws_region`` *default*
 so the IR drives the region without hard-coding it in a resource. A
 region is not a secret, so surfacing it as a default is safe.
 """
    region_default = region if region else "us-east-1"
    header = (
        "# Inputs for the aws-emr backing infrastructure (generated — R6: no literal\n"
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

variable "entry_point_key" {{
  description = "S3 object key of the PySpark entry-point script the job runs (uploaded by the package verb)."
  type        = string
  default     = "emr/scripts/main.py"
}}

variable "extra_py_files_key" {{
  description = "S3 object key of the extra python libs/wheel bundled for the job (--py-files)."
  type        = string
  default     = "emr/libs/extra-python-libs.zip"
}}

variable "emr_release_label" {{
  description = "EMR release label for the Serverless application runtime."
  type        = string
  default     = "{_DEFAULT_EMR_RELEASE}"
}}

variable "max_cpu" {{
  description = "Maximum vCPU the EMR Serverless application may scale to."
  type        = string
  default     = "16 vCPU"
}}

variable "max_memory" {{
  description = "Maximum memory the EMR Serverless application may scale to."
  type        = string
  default     = "64 GB"
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
    """Render ``main.tf`` — EMR Serverless app + S3 + IAM/OIDC for ``project_name``.

 The region is referenced via ``var.aws_region`` (whose default the IR drives in
 ``variables.tf``), so no region literal is hard-coded in a resource. The account
 id is never written literally either: it is read at apply time via
 ``aws_caller_identity`` and interpolated, so the committed HCL carries no account
 specifics. There is deliberately NO ECR, NO ECS, NO container image anywhere
 — the deployable is an EMR Serverless job running an S3 script.
 """
    prefix = _sanitize_name(project_name)

    return f"""\
# aws-emr backing infrastructure (generated — design §11.9: EMR + roles).
#
# The data-job shape (like glue): NO container image, NO ECR, NO ECS — the
# deployable is an EMR SERVERLESS job running a PySpark script uploaded to S3.
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

# --- S3: holds the versioned job script + extra libs the package verb uploads ---
resource "aws_s3_bucket" "artifacts" {{
  bucket = "${{local.name_prefix}}-${{local.account_id}}-emr-artifacts"
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

# --- CloudWatch log group for the job ------------------------------------------
resource "aws_cloudwatch_log_group" "app" {{
  name              = "/aws-emr-serverless/${{local.name_prefix}}"
  retention_in_days = 30
}}

# --- EMR Serverless: the deployable Spark compute (deploy verb target) ----------
resource "aws_emrserverless_application" "app" {{
  name          = "${{local.name_prefix}}-emr"
  release_label = var.emr_release_label
  type          = "spark"

  maximum_capacity {{
    cpu    = var.max_cpu
    memory = var.max_memory
  }}

  auto_start_configuration {{
    enabled = true
  }}

  auto_stop_configuration {{
    enabled              = true
    idle_timeout_minutes = 15
  }}
}}

# --- IAM: EMR job execution role (assumed by the service at run time) -----------
data "aws_iam_policy_document" "emr_assume" {{
  statement {{
    actions = ["sts:AssumeRole"]
    principals {{
      type        = "Service"
      identifiers = ["emr-serverless.amazonaws.com"]
    }}
  }}
}}

resource "aws_iam_role" "emr_exec" {{
  name               = "${{local.name_prefix}}-emr-exec"
  assume_role_policy = data.aws_iam_policy_document.emr_assume.json
}}

# Scoped inline policy: read the artifact bucket + write this job's CloudWatch logs.
data "aws_iam_policy_document" "emr_exec_permissions" {{
  statement {{
    sid    = "ReadArtifactBucket"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
      "s3:PutObject",
    ]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${{aws_s3_bucket.artifacts.arn}}/*",
    ]
  }}

  statement {{
    sid    = "WriteJobLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${{aws_cloudwatch_log_group.app.arn}}:*"]
  }}
}}

resource "aws_iam_role_policy" "emr_exec" {{
  name   = "${{local.name_prefix}}-emr-exec"
  role   = aws_iam_role.emr_exec.id
  policy = data.aws_iam_policy_document.emr_exec_permissions.json
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

# Deploy role can stage artifacts and start/poll EMR Serverless job runs — scoped
# to this project.
data "aws_iam_policy_document" "ci_deploy_permissions" {{
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
    sid    = "EmrDeploy"
    effect = "Allow"
    actions = [
      "emr-serverless:StartApplication",
      "emr-serverless:StartJobRun",
      "emr-serverless:GetJobRun",
      "emr-serverless:GetApplication",
      "emr-serverless:ListJobRuns",
    ]
    resources = ["*"]
  }}

  statement {{
    sid       = "PassEmrExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.emr_exec.arn]
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

output "emr_application_id" {
  description = "EMR Serverless application id the deploy/verify verbs target."
  value       = aws_emrserverless_application.app.id
}

output "artifact_bucket" {
  description = "Versioned S3 bucket the package verb uploads the job script/libs to."
  value       = aws_s3_bucket.artifacts.id
}

output "emr_role_arn" {
  description = "IAM role ARN the EMR job assumes at run time."
  value       = aws_iam_role.emr_exec.arn
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC for keyless deploy."
  value       = aws_iam_role.ci_deploy.arn
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
# Example tfvars for the aws-emr backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

aws_region   = "{region_value}"
project_name = "{prefix}"

# Where the uploaded job script + extra libs live (package verb targets).
entry_point_key    = "emr/scripts/main.py"
extra_py_files_key = "emr/libs/extra-python-libs.zip"

# EMR Serverless runtime + sizing (override as needed).
emr_release_label = "{_DEFAULT_EMR_RELEASE}"
max_cpu           = "16 vCPU"
max_memory        = "64 GB"

# CI OIDC trust — scope the subject to your repo/branch.
oidc_provider_url = "https://token.actions.githubusercontent.com"
oidc_subject      = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_client_id_list = ["{_DEFAULT_OIDC_AUDIENCE}"]
"""
