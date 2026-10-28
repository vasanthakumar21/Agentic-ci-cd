# Non-secret handles the pipeline and bootstrap consume downstream.

output "emr_application_id" {
  description = "EMR Serverless application id the deploy/verify verbs target."
  value       = aws_emrserverless_application.app.id
}

output "artifact_bucket" {
  description = "Versioned S3 bucket the package verb uploads the job script/libs to."
  value       = aws_s3_bucket.artifacts.id
}

output "emr_role_arn" {
  description = "IAM role ARN the EMR job assumes at run time."
  value       = aws_iam_role.emr_exec.arn
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC for keyless deploy."
  value       = aws_iam_role.ci_deploy.arn
}
