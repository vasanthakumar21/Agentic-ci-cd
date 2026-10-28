# Non-secret handles the pipeline and bootstrap consume downstream.

output "ecr_repository_url" {
  description = "ECR repository URL to tag/push images to (package verb)."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name (deploy/verify verbs)."
  value       = aws_ecs_cluster.app.name
}

output "ecs_service_name" {
  description = "ECS service name the deploy verb updates."
  value       = aws_ecs_service.app.name
}

output "ecs_task_family" {
  description = "ECS task definition family the deploy verb registers revisions of."
  value       = aws_ecs_task_definition.app.family
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC for keyless deploy."
  value       = aws_iam_role.ci_deploy.arn
}
