variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region to deploy resources into"
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "The deployment environment"
  type        = string
  default     = "staging"
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod"
  }
}

variable "github_owner" {
  description = "GitHub organisation or username that owns the Henchmen repository"
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name (without the owner prefix)"
  type        = string
  default     = "henchmen"
}

variable "github_default_repo" {
  description = "Default GitHub repo for operatives to work on (owner/repo format)"
  type        = string
}

variable "container_image_tag" {
  description = "Container image tag to deploy (e.g. a git short SHA or 'latest')"
  type        = string
  default     = "latest"
}
