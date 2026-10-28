# Inputs for the aws-lambda backing infrastructure (generated — R6: no literal
# account specifics or secrets; the bootstrap step fills these from local config).

variable "aws_region" {
  description = "AWS region to provision into."
  type        = string
  default     = "ap-southeast-2"
}

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
