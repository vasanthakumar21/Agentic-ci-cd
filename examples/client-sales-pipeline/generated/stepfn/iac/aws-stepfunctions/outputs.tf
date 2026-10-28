# Non-secret handles the pipeline and bootstrap consume downstream.

output "state_machine_arn" {
  description = "Step Functions state-machine ARN the deploy/verify verbs target."
  value       = aws_sfn_state_machine.app.arn
}

output "state_machine_name" {
  description = "Step Functions state-machine name."
  value       = aws_sfn_state_machine.app.name
}

output "artifact_bucket" {
  description = "Versioned S3 bucket the package verb uploads the ASL definition to."
  value       = aws_s3_bucket.artifacts.id
}

output "sfn_role_arn" {
  description = "IAM role ARN the state machine assumes at run time."
  value       = aws_iam_role.sfn_exec.arn
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC (empty under key auth)."
  value       = length(aws_iam_role.ci_deploy) > 0 ? aws_iam_role.ci_deploy[0].arn : ""
}
