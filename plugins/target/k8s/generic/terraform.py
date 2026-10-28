"""Terraform (HCL) the kubernetes plugin supplies as its backing IaC.

 — the kubernetes ``IaC`` cell is **cluster refs, RBAC**. The plugin does
NOT create a cluster (clusters are client-owned, long-lived); it renders the
*workload* as Terraform using the **kubernetes provider** so a client who manages
their cluster with Terraform can apply the same Deployment/Service through
``kubernetes_deployment`` / ``kubernetes_service``, parameterised by the IR and by
Terraform *variables* — never by literal registry credentials, tokens, or secrets.

Why variables, not literals
----------------------------
The image reference (registry + repo + tag) and the namespace differ per client and
per run. Baking them in would make the module non-portable and risk committing
environment specifics. They are therefore Terraform ``variable`` blocks the
bootstrap step fills from the local (git-ignored) config. The only value *derived
from the IR* is the project name (resource naming), which is non-secret. The
provider itself authenticates from the client's kubeconfig / in-cluster config —
no token literal is written.

Determinism
------------------
HCL is assembled from fixed, ordered string templates — no timestamps, no random
suffixes, stable block order — so regenerating from the same IR is byte-identical.
"""

from __future__ import annotations

import re


def _sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable DNS-1123 resource name prefix.

 Kubernetes object names must be a DNS-1123 label; project names are
 user-defined. We lower-case, replace any other run of characters with a single
 ``-``, and trim stray dashes so the prefix is deterministic and valid.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def render_variables_tf(region: str | None = None) -> str:
    """Render ``variables.tf`` — every client/environment input (no literals).

 Keeping these as variables (not hard-coded values) is what makes the module
 portable and keeps registry credentials / tokens / namespaces out of the
 committed files. The bootstrap step supplies them from local config.

 Args:
 region: the IR ``cloud.region`` — surfaced only as an *informational*
 ``region`` default (clusters are region-bound; a region is not a
 secret, so a default is safe, R6). It does not pin any resource.
 """
    region_default = region if region else "us-east-1"
    header = (
        "# Inputs for the kubernetes backing workload (generated — R6: no literal\n"
        "# registry credentials, tokens, or secrets; the bootstrap step fills these\n"
        "# from local config). The kubernetes provider authenticates from the\n"
        "# client's kubeconfig / in-cluster config — no token literal is written.\n"
        "\n"
        'variable "region" {\n'
        '  description = "Informational cloud region the target cluster lives in."\n'
        "  type        = string\n"
        f'  default     = "{region_default}"\n'
        "}\n"
    )
    return header + """
variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace the workload is deployed into."
  type        = string
  default     = "default"
}

variable "image_repo" {
  description = "Container image repository (registry/repo) the Deployment runs."
  type        = string
}

variable "image_tag" {
  description = "Container image tag the Deployment runs (e.g. a git SHA)."
  type        = string
  default     = "latest"
}

variable "container_port" {
  description = "Port the container listens on."
  type        = number
  default     = 8080
}

variable "replicas" {
  description = "Number of Deployment replicas to run."
  type        = number
  default     = 1
}

variable "kube_context" {
  description = "kubeconfig context to target (the provider reads kubeconfig)."
  type        = string
  default     = ""
}
"""


def render_main_tf(project_name: str) -> str:
    """Render ``main.tf`` — a kubernetes-provider Deployment + Service workload.

 Args:
 project_name: IR project name (used only for deterministic resource naming).

 The image reference and namespace are Terraform ``variable`` references (never
 literals), so no registry credential or environment specific is hard-coded. The
 provider authenticates from the client's kubeconfig/context — no token is
 committed.
 """
    prefix = _sanitize_name(project_name)

    return f"""\
# kubernetes backing workload (generated — design §8b: cluster refs, RBAC).
#
# Parameterised entirely by variables (see variables.tf). No literal registry
# credentials, tokens, or secrets appear here (R6). Resource names derive from the
# IR project name ({project_name!r}) for a stable, readable layout (R5.3). This
# module does NOT create a cluster — it deploys the workload into an existing one.

terraform {{
  required_version = ">= 1.5.0"
  required_providers {{
    kubernetes = {{
      source  = "hashicorp/kubernetes"
      version = ">= 2.0"
    }}
  }}
}}

# The provider reads the client's kubeconfig / in-cluster config. The context is a
# variable; no token or server URL literal is committed (R6).
provider "kubernetes" {{
  config_context = var.kube_context != "" ? var.kube_context : null
}}

locals {{
  name_prefix = "{prefix}"
  app_label   = "{prefix}"
}}

# --- Deployment: the workload the pipeline builds and pushes an image for -------
resource "kubernetes_deployment" "app" {{
  metadata {{
    name      = "${{local.name_prefix}}-deployment"
    namespace = var.namespace
    labels = {{
      app = local.app_label
    }}
  }}

  spec {{
    replicas = var.replicas

    selector {{
      match_labels = {{
        app = local.app_label
      }}
    }}

    template {{
      metadata {{
        labels = {{
          app = local.app_label
        }}
      }}

      spec {{
        container {{
          name  = local.app_label
          image = "${{var.image_repo}}:${{var.image_tag}}"

          port {{
            container_port = var.container_port
          }}

          resources {{
            requests = {{
              cpu    = "100m"
              memory = "128Mi"
            }}
            limits = {{
              cpu    = "500m"
              memory = "512Mi"
            }}
          }}
        }}
      }}
    }}
  }}
}}

# --- Service: stable in-cluster address fronting the Deployment pods ------------
resource "kubernetes_service" "app" {{
  metadata {{
    name      = "${{local.name_prefix}}-service"
    namespace = var.namespace
    labels = {{
      app = local.app_label
    }}
  }}

  spec {{
    selector = {{
      app = local.app_label
    }}

    port {{
      port        = 80
      target_port = var.container_port
      protocol    = "TCP"
    }}

    type = "ClusterIP"
  }}
}}
"""


def render_outputs_tf() -> str:
    """Render ``outputs.tf`` — the non-secret handles the pipeline needs downstream.

 These are the identifiers the verbs reference (deployment + service names, the
 namespace). Outputs make the module composable without re-deriving names.
 """
    return """\
# Non-secret handles the pipeline and bootstrap consume downstream.

output "deployment_name" {
  description = "Deployment name the deploy/verify verbs target (rollout status)."
  value       = kubernetes_deployment.app.metadata[0].name
}

output "service_name" {
  description = "Service name fronting the Deployment pods."
  value       = kubernetes_service.app.metadata[0].name
}

output "namespace" {
  description = "Namespace the workload is deployed into."
  value       = var.namespace
}
"""


def render_tfvars_example(project_name: str, region: str | None) -> str:
    """Render a committed ``terraform.tfvars.example`` documenting every input.

 Mirrors the ``*.example`` convention: documents the keys with
 *placeholder* values only — never real registry credentials, tokens, or
 environment specifics.
 """
    region_value = region if region else "us-east-1"
    prefix = _sanitize_name(project_name)
    return f"""\
# Example tfvars for the kubernetes backing workload (generated).
# Copy to terraform.tfvars and fill with your real (git-ignored) values.
# NO secrets or registry credentials belong in the committed example (R6).

region       = "{region_value}"
project_name = "{prefix}"
namespace    = "default"

# Image the Deployment runs — point at your registry/repo and tag.
image_repo = "registry.example.com/REPLACE_ME"
image_tag  = "latest"

# kubeconfig context to target (leave empty to use the current context).
kube_context = ""
"""
