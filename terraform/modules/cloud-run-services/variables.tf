variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region to deploy Cloud Run services into"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "labels" {
  description = "Labels to apply to Cloud Run services"
  type        = map(string)
  default     = {}
}

variable "vpc_connector_id" {
  description = "The ID of the VPC Serverless Access Connector (from networking module)"
  type        = string
}

variable "service_account_emails" {
  description = "Map of component name to service account email (from iam module). Required keys: mastermind, dispatch, forge, operative."
  type        = map(string)

  validation {
    condition     = length(setsubtract(["mastermind", "dispatch", "forge", "operative"], keys(var.service_account_emails))) == 0
    error_message = "service_account_emails must contain keys: mastermind, dispatch, forge, operative."
  }
}

variable "container_images" {
  description = "Map of component name to container image URL. Overrides the Artifact Registry default for the named components."
  type        = map(string)
  default     = {}
}

variable "placeholder_image" {
  description = "Public image deployed while container_image_tag is empty, so the first apply on a fresh project converges before any Henchmen image exists."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "github_default_repo" {
  description = "Default GitHub repository for operatives (e.g. owner/repo)"
  type        = string
  default     = ""
}

variable "pubsub_push_sa_email" {
  description = "Service account email used by Pub/Sub to authenticate push deliveries to Cloud Run"
  type        = string
}

variable "scheduler_sa_email" {
  description = "Service account email used by Cloud Scheduler to invoke Mastermind and Forge"
  type        = string
}

variable "container_image_tag" {
  description = "Container image tag to deploy (e.g. a git short SHA or 'latest'). Empty means 'images not built yet' — deploy var.placeholder_image instead."
  type        = string
  default     = ""
}

variable "firestore_database" {
  description = "Firestore database name the services must open (from the data-stores module)"
  type        = string
}

variable "gcs_bucket_dossier" {
  description = "GCS bucket name for dossier artifacts (from the data-stores module)"
  type        = string
  default     = ""
}

variable "gcs_bucket_snapshots" {
  description = "GCS bucket name for operative snapshots (from the data-stores module)"
  type        = string
  default     = ""
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
  description = <<-EOT
    Grant roles/run.invoker to allUsers on the Dispatch service.

    Required for GitHub / Jira webhooks, which cannot present a Google OIDC
    token. Dispatch verifies webhook signatures itself, but /api/v1/tasks is
    on the same service, so only enable this once that endpoint is protected
    by an application-level token.
  EOT
  type        = bool
  default     = false
}

variable "internal_only_ingress" {
  description = <<-EOT
    Restrict Mastermind and Forge to internal ingress.

    Both are only ever called by Pub/Sub push and Cloud Scheduler, which count
    as internal traffic inside the project, so the public internet has no
    reason to reach them. Set false only if a deployment fronts them with an
    external load balancer or calls them from outside the project.
  EOT
  type        = bool
  default     = true
}

variable "lair_cpu" {
  description = "vCPU allocation Mastermind requests for each operative lair job"
  type        = string
  default     = "4"
}

variable "lair_memory" {
  description = "Memory allocation Mastermind requests for each operative lair job"
  type        = string
  default     = "8Gi"
}

variable "lair_timeout" {
  description = "Maximum execution duration Mastermind sets on each operative lair job, in seconds"
  type        = number
  default     = 1800
}
