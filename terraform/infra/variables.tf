variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
  default     = "platformma"
}

variable "cluster_version" {
  description = "Kubernetes version"
  type        = string
  default     = "1.30"
}

variable "my_ip" {
  description = "Your Mac's public IP in CIDR format (e.g. 1.2.3.4/32). Used for SSH and admin access."
  type        = string
}

variable "node_instance_type" {
  description = "EC2 instance type for worker nodes"
  type        = string
  default     = "t3.medium"
}

variable "node_min_count" {
  description = "Minimum number of worker nodes"
  type        = number
  default     = 2
}

variable "node_max_count" {
  description = "Maximum number of worker nodes"
  type        = number
  default     = 4
}

variable "node_desired_count" {
  description = "Desired number of worker nodes"
  type        = number
  default     = 2
}

variable "ssh_key_name" {
  description = "EC2 key pair name for SSH access to worker nodes"
  type        = string
  default     = ""
}

variable "github_repo" {
  description = "GitHub repo URL for ArgoCD app source (e.g. https://github.com/user/repo.git)"
  type        = string
  default     = "https://github.com/EXAMPLE_REPO.git"
}

variable "state_bucket_name" {
  description = "S3 bucket name for Terraform state. Must match the backend config in versions.tf."
  type        = string
  default     = "platformma-terraform-state"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "grafana_admin_password" {
  description = "Grafana admin password"
  type        = string
  sensitive   = true
  default     = "platformma-change-me"
}

variable "prometheus_retention" {
  description = "Prometheus data retention period (e.g. 7d, 30d)"
  type        = string
  default     = "7d"
}

variable "prometheus_storage_size" {
  description = "Prometheus PVC storage size"
  type        = string
  default     = "10Gi"
}

variable "anthropic_api_key" {
  description = "Anthropic API key for the observability agents — passed via TF_VAR_anthropic_api_key in CI"
  type        = string
  sensitive   = true
}

variable "github_token" {
  description = "GitHub PAT (repo + issues scope) for the observability agents — passed via TF_VAR_github_token in CI"
  type        = string
  sensitive   = true
}

variable "agents_image_tag" {
  description = "Docker image tag for the observability agents"
  type        = string
  default     = "latest"
}
