resource "kubernetes_namespace" "argocd" {
  metadata { name = "argocd" }
  depends_on = [aws_eks_node_group.main]
}

resource "helm_release" "argocd" {
  name       = "argocd"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"
  version    = "7.7.5"
  namespace  = kubernetes_namespace.argocd.metadata[0].name

  set {
    name  = "server.service.type"
    value = "LoadBalancer"
  }

  # Expose ArgoCD server without TLS so we can reach it over HTTP on port 80
  set {
    name  = "server.insecure"
    value = "true"
  }

  # Reduce resource usage on small nodes
  set {
    name  = "redis.resources.requests.memory"
    value = "64Mi"
  }
  set {
    name  = "redis.resources.requests.cpu"
    value = "50m"
  }

  depends_on = [aws_eks_node_group.main]
}

# App of Apps bootstrap — applies a parent ArgoCD Application that watches
# helm/argocd-apps/ in the repo. ArgoCD then reconciles the individual
# Application manifests in that directory (platformma-app, monitoring).
# observability-agents is handled separately in observability-agents.tf
# because it requires the dynamic IRSA role ARN at deploy time.
resource "null_resource" "argocd_apps_bootstrap" {
  triggers = {
    repo      = var.github_repo
    cluster   = aws_eks_cluster.main.id
    argocd_rv = helm_release.argocd.metadata[0].revision
  }

  provisioner "local-exec" {
    command = <<-EOT
      aws eks update-kubeconfig --region ${var.aws_region} --name ${var.cluster_name}
      kubectl apply -f - <<'MANIFEST'
      apiVersion: argoproj.io/v1alpha1
      kind: Application
      metadata:
        name: argocd-apps
        namespace: argocd
        finalizers:
          - resources-finalizer.argocd.argoproj.io
      spec:
        project: default
        source:
          repoURL: ${var.github_repo}
          targetRevision: main
          path: helm/argocd-apps
        destination:
          server: https://kubernetes.default.svc
          namespace: argocd
        syncPolicy:
          automated:
            prune: true
            selfHeal: true
      MANIFEST
    EOT
  }

  depends_on = [helm_release.argocd]
}

# The old platformma_app null_resource is replaced by argocd_apps_bootstrap above.
# Drop it from state without deleting the ArgoCD Application already in the cluster.
removed {
  from = null_resource.platformma_app

  lifecycle {
    destroy = false
  }
}
