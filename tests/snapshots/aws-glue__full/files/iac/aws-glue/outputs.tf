# Non-secret handles the pipeline and bootstrap consume downstream.

output "glue_job_name" {
  description = "Glue job name the deploy/verify verbs target."
  value       = aws_glue_job.app.name
}

output "script_bucket" {
  description = "Versioned S3 bucket the package verb uploads the job script/libs to."
  value       = aws_s3_bucket.scripts.id
}

output "glue_role_arn" {
  description = "IAM role ARN the Glue job assumes at run time."
  value       = aws_iam_role.glue_exec.arn
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC for keyless deploy."
  value       = aws_iam_role.ci_deploy.arn
}
