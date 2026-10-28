# Non-secret handles the pipeline and bootstrap consume downstream.

output "ecr_repository_url" {
  description = "ECR repository URL to tag/push images to (package verb)."
  value       = aws_ecr_repository.app.repository_url
}

output "model_bucket" {
  description = "Versioned S3 bucket the training step uploads model artifacts to."
  value       = aws_s3_bucket.models.id
}

output "sagemaker_endpoint_name" {
  description = "SageMaker endpoint name the deploy/verify verbs target."
  value       = aws_sagemaker_endpoint.app.name
}

output "sagemaker_role_arn" {
  description = "IAM role ARN the SageMaker model assumes at run time."
  value       = aws_iam_role.sagemaker_exec.arn
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC for keyless deploy."
  value       = aws_iam_role.ci_deploy.arn
}
