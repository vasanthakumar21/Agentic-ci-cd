# Non-secret handles the pipeline and bootstrap consume downstream.

output "lambda_function_name" {
  description = "Lambda function name the deploy/verify verbs target."
  value       = aws_lambda_function.app.function_name
}

output "lambda_function_arn" {
  description = "Lambda function ARN (e.g. for a state machine to invoke)."
  value       = aws_lambda_function.app.arn
}

output "lambda_exec_role_name" {
  description = "Name of the function's execution role (for attaching extra policies)."
  value       = aws_iam_role.lambda_exec.name
}

output "lambda_alias_name" {
  description = "Lambda alias the deploy verb moves to the new version."
  value       = aws_lambda_alias.live.name
}

output "artifact_bucket" {
  description = "Versioned S3 bucket the package verb uploads the deployment zip to."
  value       = aws_s3_bucket.artifacts.id
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC (empty under key auth)."
  value       = length(aws_iam_role.ci_deploy) > 0 ? aws_iam_role.ci_deploy[0].arn : ""
}
