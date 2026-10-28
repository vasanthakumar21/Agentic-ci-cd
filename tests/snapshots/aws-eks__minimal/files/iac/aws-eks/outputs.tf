# Non-secret handles the pipeline and bootstrap consume downstream.

output "ecr_repository_url" {
  description = "ECR repository URL to tag/push images to (package verb)."
  value       = aws_ecr_repository.app.repository_url
}

output "eks_cluster_name" {
  description = "Name of the EKS cluster the workload deploys into (deploy/verify verbs)."
  value       = data.aws_eks_cluster.this.name
}

output "irsa_role_arn" {
  description = "IAM role ARN the workload ServiceAccount is annotated with (IRSA)."
  value       = aws_iam_role.workload_irsa.arn
}

output "ci_deploy_role_arn" {
  description = "IAM role ARN the CI pipeline assumes via OIDC for keyless deploy."
  value       = aws_iam_role.ci_deploy.arn
}
