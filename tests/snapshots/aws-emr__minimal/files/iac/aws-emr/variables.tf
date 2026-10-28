# Inputs for the aws-emr backing infrastructure (generated — R6: no literal
# account specifics or secrets; the bootstrap step fills these from local config).

variable "aws_region" {
  description = "AWS region to provision into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "entry_point_key" {
  description = "S3 object key of the PySpark entry-point script the job runs (uploaded by the package verb)."
  type        = string
  default     = "emr/scripts/main.py"
}

variable "extra_py_files_key" {
  description = "S3 object key of the extra python libs/wheel bundled for the job (--py-files)."
  type        = string
  default     = "emr/libs/extra-python-libs.zip"
}

variable "emr_release_label" {
  description = "EMR release label for the Serverless application runtime."
  type        = string
  default     = "emr-7.0.0"
}

variable "max_cpu" {
  description = "Maximum vCPU the EMR Serverless application may scale to."
  type        = string
  default     = "16 vCPU"
}

variable "max_memory" {
  description = "Maximum memory the EMR Serverless application may scale to."
  type        = string
  default     = "64 GB"
}

variable "oidc_provider_url" {
  description = "CI provider OIDC issuer URL (e.g. https://token.actions.githubusercontent.com)."
  type        = string
}

variable "oidc_subject" {
  description = "OIDC subject the deploy role trusts (e.g. repo:org/name:ref:refs/heads/main)."
  type        = string
}

variable "oidc_client_id_list" {
  description = "Allowed OIDC audiences for the identity provider."
  type        = list(string)
  default     = ["sts.amazonaws.com"]
}
