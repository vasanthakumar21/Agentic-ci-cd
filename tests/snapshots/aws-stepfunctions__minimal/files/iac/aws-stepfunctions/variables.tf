# Inputs for the aws-stepfunctions backing infrastructure (generated — R6: no
# literal account specifics or secrets; the bootstrap step fills these from local config).

variable "aws_region" {
  description = "AWS region to provision into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used as the resource-name prefix."
  type        = string
}

variable "definition_key" {
  description = "S3 object key of the ASL state-machine definition (uploaded by the package verb)."
  type        = string
  default     = "stepfunctions/state-machine.asl.json"
}

variable "state_machine_type" {
  description = "Step Functions state-machine type (STANDARD or EXPRESS)."
  type        = string
  default     = "STANDARD"
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

variable "use_oidc" {
  description = "Create the GitHub-OIDC provider + federated deploy role (keyless). Set false to use static access keys instead (e.g. where an org SCP denies creating an OIDC provider)."
  type        = bool
  default     = false
}

variable "enable_logging" {
  description = "Attach a CloudWatch log group + logging_configuration to the state machine. Off by default since some org SCPs restrict Logs delivery APIs."
  type        = bool
  default     = false
}

variable "task_lambda_arns" {
  description = "Lambda function ARNs the state machine is allowed to invoke. Empty grants no Lambda invoke."
  type        = list(string)
  default     = []
}
