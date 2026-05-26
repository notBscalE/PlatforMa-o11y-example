output "cluster_name" {
  description = "EKS cluster name"
  value       = aws_eks_cluster.main.name
}

output "cluster_endpoint" {
  description = "EKS cluster API endpoint"
  value       = aws_eks_cluster.main.endpoint
}

output "cluster_region" {
  description = "AWS region"
  value       = var.aws_region
}

output "kubeconfig_command" {
  description = "Run this to configure kubectl"
  value       = "aws eks update-kubeconfig --region ${var.aws_region} --name ${var.cluster_name}"
}

output "node_security_group_id" {
  description = "Security group ID for worker nodes"
  value       = aws_security_group.nodes.id
}

output "observability_agents_role_arn" {
  description = "IRSA role ARN for the observability agents"
  value       = aws_iam_role.observability_agents.arn
}

# ---------------------------------------------------------------------------
# ArgoCD URL — Terraform manages this service directly so the LB hostname
# is available via data source after the helm release is applied.
# ---------------------------------------------------------------------------

data "kubernetes_service" "argocd" {
  metadata {
    name      = "argocd-server"
    namespace = kubernetes_namespace.argocd.metadata[0].name
  }
  depends_on = [helm_release.argocd]
}

output "argocd_url" {
  description = "ArgoCD web UI URL"
  value       = "http://${data.kubernetes_service.argocd.status[0].load_balancer[0].ingress[0].hostname}"
}

# ---------------------------------------------------------------------------
# App and agents endpoints — these services are deployed by ArgoCD, not
# Terraform, so their addresses are not available here. Query them with:
# ---------------------------------------------------------------------------

output "app_health_url_command" {
  description = "Run this to get the platformma app health URL"
  value       = "kubectl get svc -n platformma platformma-platformma-app -o jsonpath='http://{.status.loadBalancer.ingress[0].hostname}/health'"
}

output "agents_health_command" {
  description = "Run this to reach the agents health endpoint"
  value       = "kubectl port-forward -n observability-agents deployment/observability-agents 8080:8080 & curl http://localhost:8080/health"
}
