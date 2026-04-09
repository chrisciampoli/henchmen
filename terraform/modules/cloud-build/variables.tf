variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region for Cloud Build triggers"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "labels" {
  description = "Labels to apply to Cloud Build resources"
  type        = map(string)
  default     = {}
}

variable "github_owner" {
  description = "GitHub organisation or username that owns the repository"
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name (without the owner prefix)"
  type        = string
}

variable "enable_cloud_build" {
  description = <<-EOT
    Whether to provision Cloud Build triggers for this environment.

    PREREQUISITE: flipping this to true requires a GitHub repository
    connection to be created manually via the GCP Console first
    (Settings > Repositories > Connect Repository). Terraform cannot manage
    the GitHub App installation, so this is a one-time manual step per
    project. Once connected, set enable_cloud_build = true in the
    environment tfvars and re-apply.
  EOT
  type        = bool
  default     = false
}
