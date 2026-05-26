##############################################################
# Observability Agents — IRSA, namespace, k8s secret, ArgoCD App
#
# ANTHROPIC_API_KEY and GITHUB_TOKEN are passed as GitHub Actions
# secrets (TF_VAR_anthropic_api_key / TF_VAR_github_token) and
# written into a Kubernetes Secret by Terraform. They never appear
# in code or state files in plaintext — Terraform marks them sensitive.
##############################################################

locals {
  agents_namespace  = "observability-agents"
  agents_sa_name    = "observability-agents"
  oidc_provider_url = replace(aws_eks_cluster.main.identity[0].oidc[0].issuer, "https://", "")
}

# Kubernetes namespace
resource "kubernetes_namespace" "observability_agents" {
  metadata { name = local.agents_namespace }
  depends_on = [aws_eks_node_group.main]
}

##############################################################
# IRSA role — grants agents CloudWatch access
##############################################################
resource "aws_iam_role" "observability_agents" {
  name = "platformma-observability-agents"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.cluster.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_provider_url}:aud" = "sts.amazonaws.com"
          "${local.oidc_provider_url}:sub" = "system:serviceaccount:${local.agents_namespace}:${local.agents_sa_name}"
        }
      }
    }]
  })
}

resource "aws_iam_policy" "observability_agents" {
  name        = "platformma-observability-agents"
  description = "Permissions for the PlatforMa observability agents"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:GetLogEvents",
          "logs:FilterLogEvents",
          "logs:StartQuery",
          "logs:StopQuery",
          "logs:GetQueryResults",
        ]
        Resource = [
          "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/eks/${var.cluster_name}/*",
          "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/eks/${var.cluster_name}/*:*",
        ]
      },
      {
        Sid    = "EKSDescribe"
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster",
          "eks:ListClusters",
          "eks:DescribeNodegroup",
          "eks:ListNodegroups",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "observability_agents" {
  role       = aws_iam_role.observability_agents.name
  policy_arn = aws_iam_policy.observability_agents.arn
}

##############################################################
# Kubernetes Secret — populated from GitHub Actions secrets
# via TF_VAR_anthropic_api_key / TF_VAR_github_token.
# Terraform marks these variables sensitive so they are never
# printed in plan/apply output.
##############################################################
resource "kubernetes_secret" "observability_agents" {
  metadata {
    name      = "observability-agents-secrets"
    namespace = kubernetes_namespace.observability_agents.metadata[0].name
  }

  data = {
    ANTHROPIC_API_KEY = var.anthropic_api_key
    GITHUB_TOKEN      = var.github_token
  }
}

import {
  to = kubernetes_secret.ghcr_pull_secret
  id = "observability-agents/ghcr-pull-secret"
}

resource "kubernetes_secret" "ghcr_pull_secret" {
  metadata {
    name      = "ghcr-pull-secret"
    namespace = kubernetes_namespace.observability_agents.metadata[0].name
  }

  type = "kubernetes.io/dockerconfigjson"

  data = {
    ".dockerconfigjson" = jsonencode({
      auths = {
        "ghcr.io" = {
          auth = base64encode("EXAMPLE_USER:${var.github_token}")
        }
      }
    })
  }
}

##############################################################
# ArgoCD Application for the agents
##############################################################
resource "null_resource" "observability_agents_app" {
  triggers = {
    cluster          = aws_eks_cluster.main.id
    argocd_rv        = helm_release.argocd.metadata[0].revision
    tag              = var.agents_image_tag
    role_arn         = aws_iam_role.observability_agents.arn
    manifest_version = "2" # bump when manifest content changes to force re-apply
  }

  provisioner "local-exec" {
    command = <<-EOT
      aws eks update-kubeconfig --region ${var.aws_region} --name ${var.cluster_name}
      kubectl apply -f - <<'MANIFEST'
      apiVersion: argoproj.io/v1alpha1
      kind: Application
      metadata:
        name: observability-agents
        namespace: argocd
        finalizers:
          - resources-finalizer.argocd.argoproj.io
      spec:
        project: default
        source:
          repoURL: ${var.github_repo}
          targetRevision: main
          path: helm/observability-agents
          helm:
            releaseName: observability-agents
            values: |
              image:
                tag: "${var.agents_image_tag}"
              serviceAccount:
                annotations:
                  eks.amazonaws.com/role-arn: "${aws_iam_role.observability_agents.arn}"
        destination:
          server: https://kubernetes.default.svc
          namespace: ${local.agents_namespace}
        syncPolicy:
          automated:
            prune: true
            selfHeal: true
          syncOptions:
            - CreateNamespace=true
      MANIFEST
    EOT
  }

  depends_on = [
    helm_release.argocd,
    kubernetes_namespace.observability_agents,
  ]
}

