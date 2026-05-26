##############################################################
# GitHub Actions OIDC — lets the pipeline assume an IAM role
# without storing long-lived credentials as secrets.
##############################################################

locals {
  github_oidc_url = "token.actions.githubusercontent.com"
  # Strip https://github.com/ prefix and .git suffix to get "org/repo"
  github_repo_path = trimsuffix(trimprefix(var.github_repo, "https://github.com/"), ".git")
  account_id       = data.aws_caller_identity.current.account_id
}

data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "github_actions" {
  url            = "https://${local.github_oidc_url}"
  client_id_list = ["sts.amazonaws.com"]
  # GitHub's OIDC thumbprint (stable — rotated via GitHub announcement)
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1",
  "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}

##############################################################
# IAM Role
##############################################################
resource "aws_iam_role" "github_actions" {
  name = "platformma-github-actions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github_actions.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.github_oidc_url}:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Allow any ref (push to main + PRs)
          "${local.github_oidc_url}:sub" = "repo:${local.github_repo_path}:*"
        }
      }
    }]
  })
}

##############################################################
# IAM Policy — scoped to what Terraform actually manages
##############################################################
resource "aws_iam_policy" "github_actions" {
  name        = "platformma-github-actions"
  description = "Permissions for the PlatforMa CI/CD pipeline"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # ── S3: Terraform state bucket only ──────────────────
      {
        Sid    = "TerraformStateBucket"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = "arn:aws:s3:::${var.state_bucket_name}/*"
      },
      {
        Sid      = "TerraformStateBucketList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketVersioning"]
        Resource = "arn:aws:s3:::${var.state_bucket_name}"
      },

      # ── EC2: VPC, subnets, SGs, IGW, route tables ────────
      {
        Sid    = "EC2Networking"
        Effect = "Allow"
        Action = [
          "ec2:Describe*",
          "ec2:CreateVpc", "ec2:DeleteVpc", "ec2:ModifyVpcAttribute",
          "ec2:CreateSubnet", "ec2:DeleteSubnet", "ec2:ModifySubnetAttribute",
          "ec2:CreateInternetGateway", "ec2:DeleteInternetGateway",
          "ec2:AttachInternetGateway", "ec2:DetachInternetGateway",
          "ec2:CreateRouteTable", "ec2:DeleteRouteTable",
          "ec2:CreateRoute", "ec2:DeleteRoute",
          "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable",
          "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
          "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress",
          "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress",
          "ec2:CreateTags", "ec2:DeleteTags",
        ]
        Resource = "*"
      },

      # ── EKS: cluster + node group lifecycle ──────────────
      {
        Sid    = "EKS"
        Effect = "Allow"
        Action = ["eks:*"]
        Resource = [
          "arn:aws:eks:${var.aws_region}:${local.account_id}:cluster/${var.cluster_name}",
          "arn:aws:eks:${var.aws_region}:${local.account_id}:nodegroup/${var.cluster_name}/*",
          "arn:aws:eks:${var.aws_region}:${local.account_id}:addon/${var.cluster_name}/*",
          "arn:aws:eks:${var.aws_region}:${local.account_id}:access-entry/${var.cluster_name}/*",
          "arn:aws:eks::aws:cluster-access-policy/*",
        ]
      },
      {
        Sid      = "EKSList"
        Effect   = "Allow"
        Action   = ["eks:ListClusters", "eks:DescribeAddonVersions"]
        Resource = "*"
      },

      # ── IAM: roles scoped to platformma- prefix ───────────
      {
        Sid    = "IAMRoles"
        Effect = "Allow"
        Action = [
          "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole",
          "iam:TagRole", "iam:UntagRole",
          "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
          "iam:AttachRolePolicy", "iam:DetachRolePolicy",
          "iam:PassRole",
        ]
        Resource = "arn:aws:iam::${local.account_id}:role/platformma-*"
      },
      {
        Sid    = "IAMPolicies"
        Effect = "Allow"
        Action = [
          "iam:CreatePolicy", "iam:DeletePolicy",
          "iam:GetPolicy", "iam:GetPolicyVersion",
          "iam:ListPolicyVersions",
          "iam:CreatePolicyVersion", "iam:DeletePolicyVersion",
          "iam:TagPolicy", "iam:UntagPolicy",
        ]
        Resource = "arn:aws:iam::${local.account_id}:policy/platformma-*"
      },
      {
        Sid    = "IAMOIDCProviders"
        Effect = "Allow"
        Action = [
          "iam:CreateOpenIDConnectProvider", "iam:DeleteOpenIDConnectProvider",
          "iam:GetOpenIDConnectProvider", "iam:UpdateOpenIDConnectProvider",
          "iam:TagOpenIDConnectProvider", "iam:UntagOpenIDConnectProvider",
          "iam:ListOpenIDConnectProviders",
        ]
        Resource = "*"
      },

      # ── ELB: created by Kubernetes LoadBalancer services ─
      {
        Sid    = "ELB"
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:Describe*",
          "elasticloadbalancing:CreateLoadBalancer",
          "elasticloadbalancing:DeleteLoadBalancer",
          "elasticloadbalancing:ModifyLoadBalancerAttributes",
          "elasticloadbalancing:CreateListener", "elasticloadbalancing:DeleteListener",
          "elasticloadbalancing:CreateTargetGroup", "elasticloadbalancing:DeleteTargetGroup",
          "elasticloadbalancing:RegisterTargets", "elasticloadbalancing:DeregisterTargets",
          "elasticloadbalancing:AddTags", "elasticloadbalancing:RemoveTags",
        ]
        Resource = "*"
      },

      # ── Auto Scaling: managed node group scaling ──────────
      {
        Sid    = "AutoScaling"
        Effect = "Allow"
        Action = [
          "autoscaling:Describe*",
          "autoscaling:CreateAutoScalingGroup", "autoscaling:DeleteAutoScalingGroup",
          "autoscaling:UpdateAutoScalingGroup",
          "autoscaling:CreateLaunchConfiguration", "autoscaling:DeleteLaunchConfiguration",
          "autoscaling:CreateOrUpdateTags",
        ]
        Resource = "*"
      },
    ]
  })
}

# Grant the GitHub Actions role cluster-admin access to the EKS Kubernetes API
resource "aws_eks_access_entry" "github_actions" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_iam_role.github_actions.arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "github_actions" {
  cluster_name  = aws_eks_cluster.main.name
  principal_arn = aws_iam_role.github_actions.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.github_actions]
}

resource "aws_iam_role_policy_attachment" "github_actions" {
  role       = aws_iam_role.github_actions.name
  policy_arn = aws_iam_policy.github_actions.arn
}

output "github_actions_role_arn" {
  description = "ARN to set as GHA_ROLE_ARN in GitHub Actions variables"
  value       = aws_iam_role.github_actions.arn
}
