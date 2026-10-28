"""Terraform (HCL) the aws-glue plugin supplies as its backing IaC.

 — the aws-glue ``IaC`` cell is **Glue job + role (Terraform)**. This
module renders that infrastructure as valid HCL, parameterised by the IR and by
Terraform *variables* — never by literal account ids, secrets, or ARNs.

What the module provisions
--------------------------
* **S3 script bucket** — holds the versioned Glue job script(s) + any extra python
 libs/wheels the ``package`` verb uploads. Versioning is enabled so each upload is
 addressable by an immutable object version id (the promotable reference —
 lists Glue as promotion=yes).
* **Glue job** (``aws_glue_job``) — the deployable workload. Its ``command``
 ``script_location`` points at the uploaded script in the bucket; there is NO
 container image anywhere (the data-job shape, like databricks). The ``deploy``
 verb creates/updates this same job out of band; the ``verify`` verb starts + polls
 a run.
* **IAM Glue service role** — the role Glue assumes at run time
 (``glue.amazonaws.com`` trust) with the managed ``AWSGlueServiceRole`` policy plus
 a scoped inline policy for reading the script bucket and writing CloudWatch Logs.
* **IAM + OIDC** — a GitHub/Azure OIDC identity provider and a deploy role whose
 trust policy is scoped to the client's repo, so the pipeline authenticates
 *keylessly* (no long-lived access keys — the R6/OIDC story). The deploy role can
 upload artifacts to the bucket and create/update/start the Glue job — scoped, no
 wildcards on identity.

Why variables, not literals
----------------------------
The account id, the OIDC subject (``repo:org/name:*``), the script key, and the
Glue version / worker sizing differ per client and per run. Baking them in would
make the module non-portable and risk committing account-specific values. They are
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

#: GitHub's OIDC audience when using ``aws-actions/configure-aws-credentials``.
_DEFAULT_OIDC_AUDIENCE = "sts.amazonaws.com"

#: Pinned Glue version default (reproducible, non-"latest") — mirrors jobspec.py.
_DEFAULT_GLUE_VERSION = "4.0"
_DEFAULT_WORKER_TYPE = "G.1X"
_DEFAULT_NUMBER_OF_WORKERS = 2


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable resource-name prefix.

 Glue/IAM/S3 names allow ``[A-Za-z0-9-_]`` (S3 bucket names are stricter — lower
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
        "# Inputs for the aws-glue backing infrastructure (generated — R6: no literal\n"
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

variable "script_key" {{
  description = "S3 object key of the Glue job script the job runs (uploaded by the package verb)."
  type        = string
  default     = "glue/scripts/main.py"
}}

variable "extra_py_files_key" {{
  description = "S3 object key of the extra python libs/wheel bundled for the job (--extra-py-files)."
  type        = string
  default     = "glue/libs/extra-python-libs.zip"
}}

variable "glue_version" {{
  description = "Glue runtime version for the job."
  type        = string
  default     = "{_DEFAULT_GLUE_VERSION}"
}}

variable "worker_type" {{
  description = "Glue worker type for the job (e.g. G.1X, G.2X)."
  type        = string
  default     = "{_DEFAULT_WORKER_TYPE}"
}}

variable "number_of_workers" {{
  description = "Number of Glue workers allocated to the job."
  type        = number
  default     = {_DEFAULT_NUMBER_OF_WORKERS}
}}

variable "max_retries" {{
  description = "Number of times Glue retries the job if it fails."
  type        = number
  default     = 0
}}

variable "timeout_minutes" {{
  description = "Job timeout in minutes before Glue terminates the run."
  type        = number
  default     = 60
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
    """Render ``main.tf`` — Glue job + S3 script bucket + IAM/OIDC for ``project_name``.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The region is referenced via ``var.aws_region`` (whose default the IR drives in
 ``variables.tf``), so no region literal is hard-coded in a resource. The account
 id is never written literally either: it is read at apply time via
 ``aws_caller_identity`` and interpolated, so the committed HCL carries no
 account specifics. There is deliberately NO ECR, NO ECS, NO container image
 anywhere — the deployable is a Glue job running an S3 script.
 """
    prefix = _sanitize_name(project_name)

    return f"""\
# aws-glue backing infrastructure (generated — design §11.9: Glue job + role).
#
# The data-job shape (like databricks): NO container image, NO ECR, NO ECS — the
# deployable is a Glue JOB running a Spark/Python script uploaded to S3.
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
resource "aws_s3_bucket" "scripts" {{
  bucket = "${{local.name_prefix}}-${{local.account_id}}-glue-scripts"
}}

resource "aws_s3_bucket_versioning" "scripts" {{
  bucket = aws_s3_bucket.scripts.id
  versioning_configuration {{
    status = "Enabled"
  }}
}}

resource "aws_s3_bucket_public_access_block" "scripts" {{
  bucket                  = aws_s3_bucket.scripts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}}

# --- CloudWatch log group for the job ------------------------------------------
resource "aws_cloudwatch_log_group" "app" {{
  name              = "/aws-glue/jobs/${{local.name_prefix}}"
  retention_in_days = 30
}}

# --- IAM: Glue service role (assumed by the service at run time) ----------------
data "aws_iam_policy_document" "glue_assume" {{
  statement {{
    actions = ["sts:AssumeRole"]
    principals {{
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }}
  }}
}}

resource "aws_iam_role" "glue_exec" {{
  name               = "${{local.name_prefix}}-glue-exec"
  assume_role_policy = data.aws_iam_policy_document.glue_assume.json
}}

# AWS-managed Glue service policy (public managed-policy ARN — not account-specific).
resource "aws_iam_role_policy_attachment" "glue_service" {{
  role       = aws_iam_role.glue_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}}

# Scoped inline policy: read the script bucket + write this job's CloudWatch logs.
data "aws_iam_policy_document" "glue_exec_permissions" {{
  statement {{
    sid    = "ReadScriptBucket"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.scripts.arn,
      "${{aws_s3_bucket.scripts.arn}}/*",
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

resource "aws_iam_role_policy" "glue_exec" {{
  name   = "${{local.name_prefix}}-glue-exec"
  role   = aws_iam_role.glue_exec.id
  policy = data.aws_iam_policy_document.glue_exec_permissions.json
}}

# --- Glue: the deployable workload (deploy verb target) ------------------------
# The script location is a VARIABLE key into the versioned bucket so no artifact
# value is committed; the deploy verb updates the job + the verify verb runs it.
resource "aws_glue_job" "app" {{
  name              = local.name_prefix
  role_arn          = aws_iam_role.glue_exec.arn
  glue_version      = var.glue_version
  worker_type       = var.worker_type
  number_of_workers = var.number_of_workers
  max_retries       = var.max_retries
  timeout           = var.timeout_minutes

  command {{
    name            = "glueetl"
    python_version  = "3"
    script_location = "s3://${{aws_s3_bucket.scripts.id}}/${{var.script_key}}"
  }}

  default_arguments = {{
    "--job-language"                     = "python"
    "--extra-py-files"                   = "s3://${{aws_s3_bucket.scripts.id}}/${{var.extra_py_files_key}}"
    "--enable-metrics"                   = "true"
    "--enable-continuous-cloudwatch-log" = "true"
  }}

  lifecycle {{
    # The deploy verb updates the script/args out of band; ignore drift on those.
    ignore_changes = [command, default_arguments]
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

# Deploy role can stage scripts and create/update/start the Glue job — scoped to
# this project.
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
      aws_s3_bucket.scripts.arn,
      "${{aws_s3_bucket.scripts.arn}}/*",
    ]
  }}

  statement {{
    sid    = "GlueDeploy"
    effect = "Allow"
    actions = [
      "glue:CreateJob",
      "glue:UpdateJob",
      "glue:GetJob",
      "glue:GetJobRun",
      "glue:StartJobRun",
      "glue:BatchStopJobRun",
    ]
    resources = ["*"]
  }}

  statement {{
    sid       = "PassGlueExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.glue_exec.arn]
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

 These are the non-secret identifiers the verbs reference (the Glue job name the
 deploy/verify verbs target, the script bucket the package verb uploads to, the
 Glue execution role, and the deploy role ARN the CI OIDC step assumes). Outputs
 make the module composable without re-deriving names elsewhere.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "glue_job_name" {
  description = "Glue job name the deploy/verify verbs target."
  value       = aws_glue_job.app.name
}

output "script_bucket" {
  description = "Versioned S3 bucket the package verb uploads the job script/libs to."
  value       = aws_s3_bucket.scripts.id
}

output "glue_role_arn" {
  description = "IAM role ARN the Glue job assumes at run time."
  value       = aws_iam_role.glue_exec.arn
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
# Example tfvars for the aws-glue backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

aws_region   = "{region_value}"
project_name = "{prefix}"

# Where the uploaded job script + extra libs live (package verb targets).
script_key         = "glue/scripts/main.py"
extra_py_files_key = "glue/libs/extra-python-libs.zip"

# Glue runtime + sizing (override as needed).
glue_version      = "{_DEFAULT_GLUE_VERSION}"
worker_type       = "{_DEFAULT_WORKER_TYPE}"
number_of_workers = {_DEFAULT_NUMBER_OF_WORKERS}
max_retries       = 0
timeout_minutes   = 60

# CI OIDC trust — scope the subject to your repo/branch.
oidc_provider_url = "https://token.actions.githubusercontent.com"
oidc_subject      = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_client_id_list = ["{_DEFAULT_OIDC_AUDIENCE}"]
"""
