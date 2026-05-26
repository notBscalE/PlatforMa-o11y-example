resource "kubernetes_namespace" "monitoring" {
  metadata { name = "monitoring" }
  depends_on = [aws_eks_node_group.main]
}

# Grafana admin credentials — referenced by kube-prometheus-stack via
# grafana.admin.existingSecret in helm/monitoring/values.yaml.
resource "kubernetes_secret" "grafana_admin" {
  metadata {
    name      = "grafana-admin-secret"
    namespace = kubernetes_namespace.monitoring.metadata[0].name
  }

  data = {
    admin-user     = "admin"
    admin-password = var.grafana_admin_password
  }
}

# kube-prometheus-stack is now managed by ArgoCD (helm/argocd-apps/monitoring.yaml).
# This removed block tells Terraform to drop the old helm_release from state
# without destroying the running release.
removed {
  from = helm_release.kube_prometheus_stack

  lifecycle {
    destroy = false
  }
}
