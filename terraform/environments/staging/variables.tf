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
  description = "The deployment environment. Set in staging.auto.tfvars."
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
  description = "Container image tag to deploy. Empty means the images are not built yet and a public placeholder image is deployed instead."
  type        = string
  default     = ""
}

variable "container_images" {
  description = "Per-component container image overrides (keys: mastermind, dispatch, forge)"
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------
# Intake configuration
# ---------------------------------------------------------------------------

variable "jira_base_url" {
  description = "Jira base URL (e.g. https://acme.atlassian.net). Empty disables the Jira intake env on Dispatch."
  type        = string
  default     = ""
}

variable "jira_email" {
  description = "Jira account email used with the Jira API token"
  type        = string
  default     = ""
}

variable "internal_only_ingress" {
  description = "Restrict Mastermind and Forge to internal ingress (Pub/Sub push and Cloud Scheduler count as internal)"
  type        = bool
  default     = true
}

variable "dispatch_public_ingress" {
  description = "Allow unauthenticated invocation of Dispatch (required for GitHub / Jira webhooks)"
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Per-environment overrides (values in staging.auto.tfvars).
# ---------------------------------------------------------------------------

variable "lair_cpu" {
  description = "CPU limit for each Operative Lair container"
  type        = string
}

variable "lair_memory" {
  description = "Memory limit for each Operative Lair container"
  type        = string
}

variable "lair_timeout" {
  description = "Maximum execution duration for a Lair job, in seconds"
  type        = number
  default     = 1800
}

variable "scheduler_enabled" {
  description = "Whether to provision periodic Cloud Scheduler jobs"
  type        = bool
}

variable "enable_cloud_build" {
  description = "Whether to provision Cloud Build triggers"
  type        = bool
  default     = false
}

variable "enable_cloud_build_notifications" {
  description = "Create the push subscription from the project's `cloud-builds` topic to Forge"
  type        = bool
  default     = false
}

variable "seed_secret_placeholders" {
  description = "Seed every Secret Manager secret with a placeholder version. Set false once the secrets hold real values."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "Retention, in days, for the Henchmen log bucket"
  type        = number
  default     = 30
}

variable "artifact_retention_days" {
  description = "Days after which dossier artifacts and operative snapshots are deleted"
  type        = number
  default     = 90
}
