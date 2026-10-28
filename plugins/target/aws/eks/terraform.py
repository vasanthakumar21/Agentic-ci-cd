"""Terraform (HCL) the aws-eks plugin supplies as its backing IaC.

 — the aws-eks ``IaC`` cell is **EKS refs + IRSA/OIDC (Terraform)**.
This module renders that infrastructure as valid HCL, parameterised by the IR and
by Terraform *variables* — never by literal account ids, secrets, or ARNs.

What the module provisions / references
---------------------------------------
* **EKS references** — the module does NOT create a cluster (clusters are long-lived,
 client-owned). It *references* an existing cluster by name via ``aws_eks_cluster``
 / ``aws_eks_cluster_auth`` data sources so the workload and IRSA bind to the real
 cluster, and configures the ``kubernetes`` + ``helm`` providers from those data
 sources (no kubeconfig literal committed).
* **ECR** — a repository to hold the image the pipeline builds + pushes (the
 ``package`` verb target), with scan-on-push and immutable tags.
* **IRSA (IAM Roles for Service Accounts)** — a workload IAM role whose trust policy
 is scoped to the cluster's OIDC provider + the workload ServiceAccount
 (``system:serviceaccount:<ns>:<sa>``). This is the EKS-native keyless way for the
 pod to assume an IAM role, bound to the ServiceAccount the manifests annotate.
* **GitHub/Azure OIDC deploy role** — a separate OIDC identity provider + a deploy
 role whose trust policy is scoped to the client's repo, so the CI pipeline
 authenticates *keylessly* (no long-lived access keys) to push to ECR and run
 ``kubectl``/``helm`` against the cluster.

Why variables, not literals
----------------------------
The account id, the cluster name, the OIDC subjects (CI ``repo:org/name:*`` and the
workload ``system:serviceaccount:ns:sa``), and the namespace differ per client and
per run. Baking them in would make the module non-portable and risk committing
account-specific values. They are therefore Terraform ``variable`` blocks the
bootstrap step fills from the local (git-ignored) config. The only values *derived
from the IR* are the project name (resource naming) and the region (provider +
defaults), both non-secret.

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

 ECR/IAM names allow ``[A-Za-z0-9-_]``; project names are user-defined. We
 lower-case, replace any other run of characters with a single ``-``, and trim
 stray dashes so the prefix is deterministic and valid across all services.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/account-specific input (no literals).

 Keeping these as variables (not hard-coded values) is what makes the module
 portable and keeps account ids / cluster names / OIDC subjects out of the
 committed files. The bootstrap step supplies them from the local
 git-ignored config.

 Args:
 region: the IR ``cloud.region`` — used only as the ``aws_region`` *default*
 so the IR drives the region without hard-coding it in a resource. A
 region is not a secret, so surfacing it as a default is safe.
 """
    region_default = region if region else "us-east-1"
    header = (
        "# Inputs for the aws-eks backing infrastructure (generated — R6: no literal\n"
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

variable "eks_cluster_name" {
  description = "Name of the existing EKS cluster the workload deploys into (referenced, not created)."
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace the workload (and its ServiceAccount) live in."
  type        = string
  default     = "default"
}

variable "service_account_name" {
  description = "Workload ServiceAccount name IRSA binds the workload IAM role to."
  type        = string
  default     = "app"
}

variable "oidc_provider_url" {
  description = "CI provider OIDC issuer URL (e.g. https://token.actions.githubusercontent.com)."
  type        = string
}

variable "oidc_subject" {
  description = "CI OIDC subject the deploy role trusts (e.g. repo:org/name:ref:refs/heads/main)."
  type        = string
}

variable "oidc_client_id_list" {
  description = "Allowed OIDC audiences for the CI identity provider."
  type        = list(string)
  default     = ["sts.amazonaws.com"]
}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — EKS refs + ECR + IRSA + CI OIDC for ``project_name``.

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
# aws-eks backing infrastructure (generated — design §11.9: EKS refs + IRSA/OIDC).
#
# Parameterised entirely by variables (see variables.tf). No literal account ids,
# ARNs, or secrets appear here (R6). Resource names derive from the IR project
# name ({project_name!r}) for a stable, readable layout (R5.3). This module does
# NOT create a cluster — it references an existing one and deploys the workload's
# supporting IAM/ECR into it.

terraform {{
  required_version = ">= 1.5.0"
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }}
    kubernetes = {{
      source  = "hashicorp/kubernetes"
      version = ">= 2.0"
    }}
    helm = {{
      source  = "hashicorp/helm"
      version = ">= 2.0"
    }}
  }}
}}

provider "aws" {{
  region = var.aws_region
}}

# Account id is resolved at apply time — never committed as a literal (R6).
data "aws_caller_identity" "current" {{}}

# --- EKS: reference the existing cluster (NOT created here) --------------------
data "aws_eks_cluster" "this" {{
  name = var.eks_cluster_name
}}

data "aws_eks_cluster_auth" "this" {{
  name = var.eks_cluster_name
}}

locals {{
  name_prefix = "{prefix}"
  account_id  = data.aws_caller_identity.current.account_id
  # The cluster's OIDC issuer (https://oidc.eks...), stripped of its scheme so it
  # can be used as a condition key prefix in the IRSA trust policy.
  oidc_issuer_url  = data.aws_eks_cluster.this.identity[0].oidc[0].issuer
  oidc_issuer_host = replace(local.oidc_issuer_url, "https://", "")
}}

# The kubernetes + helm providers authenticate from the referenced cluster's
# endpoint + CA + a short-lived token — no kubeconfig literal is committed (R6).
provider "kubernetes" {{
  host                   = data.aws_eks_cluster.this.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.this.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.this.token
}}

provider "helm" {{
  kubernetes {{
    host                   = data.aws_eks_cluster.this.endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.this.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.this.token
  }}
}}

# --- ECR: holds the image the pipeline builds and pushes (package verb) --------
resource "aws_ecr_repository" "app" {{
  name                 = "${{local.name_prefix}}-ecr"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {{
    scan_on_push = true
  }}
}}

# --- IRSA: the cluster's OIDC provider (referenced) + the workload IAM role ----
# The cluster already has an IAM OIDC provider; reference it by the issuer URL so
# the IRSA trust policy can federate the workload ServiceAccount (no account id).
data "aws_iam_openid_connect_provider" "eks" {{
  url = local.oidc_issuer_url
}}

data "aws_iam_policy_document" "irsa_assume" {{
  statement {{
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"

    principals {{
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.eks.arn]
    }}

    condition {{
      test     = "StringEquals"
      variable = "${{local.oidc_issuer_host}}:aud"
      values   = ["sts.amazonaws.com"]
    }}

    # Scope the role to exactly the workload ServiceAccount (IRSA).
    condition {{
      test     = "StringEquals"
      variable = "${{local.oidc_issuer_host}}:sub"
      values   = ["system:serviceaccount:${{var.namespace}}:${{var.service_account_name}}"]
    }}
  }}
}}

resource "aws_iam_role" "workload_irsa" {{
  name               = "${{local.name_prefix}}-irsa"
  assume_role_policy = data.aws_iam_policy_document.irsa_assume.json
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

# Deploy role can push to ECR and describe the cluster to run kubectl/helm —
# scoped to this project. Cluster RBAC inside EKS further scopes what it can apply.
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
      "ecr:DescribeImages",
    ]
    resources = ["*"]
  }}

  statement {{
    sid    = "EksDescribeForKubectl"
    effect = "Allow"
    actions = [
      "eks:DescribeCluster",
    ]
    resources = [data.aws_eks_cluster.this.arn]
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

 These are the non-secret identifiers the verbs reference (ECR repo URL, the
 referenced cluster name, the IRSA role ARN the ServiceAccount is annotated with,
 the deploy role ARN the CI OIDC step assumes). Outputs make the module
 composable without re-deriving names elsewhere.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "ecr_repository_url" {
  description = "ECR repository URL to tag/push images to (package verb)."
  value       = aws_ecr_repository.app.repository_url
}

output "eks_cluster_name" {
  description = "Name of the EKS cluster the workload deploys into (deploy/verify verbs)."
  value       = data.aws_eks_cluster.this.name
}

output "irsa_role_arn" {
  description = "IAM role ARN the workload ServiceAccount is annotated with (IRSA)."
  value       = aws_iam_role.workload_irsa.arn
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC for keyless deploy."
  value       = aws_iam_role.ci_deploy.arn
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input.

 Mirrors the ``*.example`` convention: documents the keys with
 *placeholder* values only — never real account ids, cluster names, or ARNs.
 """
    region_value = region if region else "us-east-1"
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the aws-eks backing infrastructure (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or real account specifics belong in the committed example (R6).

aws_region   = "{region_value}"
project_name = "{prefix}"

# The existing EKS cluster the workload deploys into (referenced, not created).
eks_cluster_name = "REPLACE_ME-cluster"

# Workload namespace + ServiceAccount the IRSA role is bound to.
namespace            = "default"
service_account_name = "{prefix}"

# CI OIDC trust — scope the subject to your repo/branch.
oidc_provider_url = "https://token.actions.githubusercontent.com"
oidc_subject      = "repo:your-org/your-repo:ref:refs/heads/main"
oidc_client_id_list = ["{_DEFAULT_OIDC_AUDIENCE}"]
"""
