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
  description = "The deployment environment. Must be set by the calling env directory."
  type        = string
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
  description = <<-EOT
    Container image tag to deploy (e.g. a git short SHA or 'latest').

    Empty (the default) means the Henchmen images have not been pushed yet:
    every service deploys var.placeholder_image so the first apply on a fresh
    project converges. Build and push the images, then set this and re-apply.
  EOT
  type        = string
  default     = ""
}

variable "container_images" {
  description = "Per-component container image overrides (keys: mastermind, dispatch, forge). Wins over container_image_tag."
  type        = map(string)
  default     = {}
}

variable "placeholder_image" {
  description = "Public image deployed while container_image_tag is empty"
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "seed_secret_placeholders" {
  description = "Seed every Secret Manager secret with a placeholder version so the first apply produces startable revisions. Set false on projects whose secrets already hold real values."
  type        = bool
  default     = true
}

variable "jira_base_url" {
  description = "Jira base URL (e.g. https://acme.atlassian.net). Empty disables the Jira intake env on Dispatch."
  type        = string
  default     = ""
}

variable "jira_email" {
  description = "Jira account email used with the Jira API token. Empty disables the Jira intake env on Dispatch."
  type        = string
  default     = ""
}

variable "dispatch_public_ingress" {
  description = "Allow unauthenticated invocation of Dispatch (required for GitHub / Jira webhooks, which cannot present a Google OIDC token)."
  type        = bool
  default     = false
}

variable "internal_only_ingress" {
  description = "Restrict Mastermind and Forge to internal ingress (Pub/Sub push and Cloud Scheduler count as internal)"
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

# ---------------------------------------------------------------------------
# Per-environment overrides. These have no defaults at the root level — each
# env directory must set them via its `*.auto.tfvars`. This is the whole point
# of the root module: shared composition, env-specific values.
# ---------------------------------------------------------------------------

variable "lair_cpu" {
  description = "CPU limit for each Operative Lair container (vCPU)"
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
  description = "Whether to provision periodic Cloud Scheduler jobs (watchdog, merge queue, etc.)"
  type        = bool
}

variable "enable_cloud_build" {
  description = "Whether to provision Cloud Build triggers. Requires manual GitHub repo connection via GCP Console first."
  type        = bool
  default     = false
}

variable "enable_cloud_build_notifications" {
  description = "Create the push subscription from the project's `cloud-builds` topic to Forge. Requires at least one Cloud Build run to have created that topic."
  type        = bool
  default     = false
}
