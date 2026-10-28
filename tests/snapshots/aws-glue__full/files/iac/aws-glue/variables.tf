# Inputs for the aws-glue backing infrastructure (generated — R6: no literal
# account specifics or secrets; the bootstrap step fills these from local config).

variable "aws_region" {
  description = "AWS region to provision into."
  type        = string
  default     = "eu-west-2"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "script_key" {
  description = "S3 object key of the Glue job script the job runs (uploaded by the package verb)."
  type        = string
  default     = "glue/scripts/main.py"
}

variable "extra_py_files_key" {
  description = "S3 object key of the extra python libs/wheel bundled for the job (--extra-py-files)."
  type        = string
  default     = "glue/libs/extra-python-libs.zip"
}

variable "glue_version" {
  description = "Glue runtime version for the job."
  type        = string
  default     = "4.0"
}

variable "worker_type" {
  description = "Glue worker type for the job (e.g. G.1X, G.2X)."
  type        = string
  default     = "G.1X"
}

variable "number_of_workers" {
  description = "Number of Glue workers allocated to the job."
  type        = number
  default     = 2
}

variable "max_retries" {
  description = "Number of times Glue retries the job if it fails."
  type        = number
  default     = 0
}

variable "timeout_minutes" {
  description = "Job timeout in minutes before Glue terminates the run."
  type        = number
  default     = 60
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
