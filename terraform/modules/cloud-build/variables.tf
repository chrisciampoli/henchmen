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
