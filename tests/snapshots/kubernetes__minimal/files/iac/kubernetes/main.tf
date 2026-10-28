# kubernetes backing workload (generated — design §8b: cluster refs, RBAC).
#
# Parameterised entirely by variables (see variables.tf). No literal registry
# credentials, tokens, or secrets appear here (R6). Resource names derive from the
# IR project name ('minimal-svc') for a stable, readable layout (R5.3). This
# module does NOT create a cluster — it deploys the workload into an existing one.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.0"
    }
  }
}

# The provider reads the client's kubeconfig / in-cluster config. The context is a
# variable; no token or server URL literal is committed (R6).
provider "kubernetes" {
  config_context = var.kube_context != "" ? var.kube_context : null
}

locals {
  name_prefix = "minimal-svc"
  app_label   = "minimal-svc"
}

# --- Deployment: the workload the pipeline builds and pushes an image for -------
resource "kubernetes_deployment" "app" {
  metadata {
    name      = "${local.name_prefix}-deployment"
    namespace = var.namespace
    labels = {
      app = local.app_label
    }
  }

  spec {
    replicas = var.replicas

    selector {
      match_labels = {
        app = local.app_label
      }
    }

    template {
      metadata {
        labels = {
          app = local.app_label
        }
      }

      spec {
        container {
          name  = local.app_label
          image = "${var.image_repo}:${var.image_tag}"

          port {
            container_port = var.container_port
          }

          resources {
            requests = {
              cpu    = "100m"
              memory = "128Mi"
            }
            limits = {
              cpu    = "500m"
              memory = "512Mi"
            }
          }
        }
      }
    }
  }
}

# --- Service: stable in-cluster address fronting the Deployment pods ------------
resource "kubernetes_service" "app" {
  metadata {
    name      = "${local.name_prefix}-service"
    namespace = var.namespace
    labels = {
      app = local.app_label
    }
  }

  spec {
    selector = {
      app = local.app_label
    }

    port {
      port        = 80
      target_port = var.container_port
      protocol    = "TCP"
    }

    type = "ClusterIP"
  }
}
