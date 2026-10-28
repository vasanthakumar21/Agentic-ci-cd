# Non-secret handles the pipeline and bootstrap consume downstream.

output "ecr_repository_url" {
  description = "ECR repository URL to tag/push images to (package verb)."
  value       = aws_ecr_repository.app.repository_url
}

output "batch_job_queue" {
  description = "AWS Batch job queue the verify verb submits jobs to."
  value       = aws_batch_job_queue.app.name
}

output "batch_job_definition" {
  description = "AWS Batch job definition the deploy verb registers revisions of."
  value       = aws_batch_job_definition.app.name
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC for keyless deploy."
  value       = aws_iam_role.ci_deploy.arn
}
