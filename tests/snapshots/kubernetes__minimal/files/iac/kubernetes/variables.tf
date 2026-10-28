# Inputs for the kubernetes backing workload (generated — R6: no literal
# registry credentials, tokens, or secrets; the bootstrap step fills these
# from local config). The kubernetes provider authenticates from the
# client's kubeconfig / in-cluster config — no token literal is written.

variable "region" {
  description = "Informational cloud region the target cluster lives in."
  type        = string
  default     = "us-east-1"
}

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
