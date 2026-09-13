variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "service_account_emails" {
  description = "Map of service name to service account email (from the iam module outputs). Required keys: mastermind, dispatch, operative, forge."
  type        = map(string)

  validation {
    condition     = length(setsubtract(["mastermind", "dispatch", "operative", "forge"], keys(var.service_account_emails))) == 0
    error_message = "service_account_emails must contain keys: mastermind, dispatch, operative, forge."
  }
}

variable "labels" {
  description = "Labels to apply to Secret Manager secrets"
  type        = map(string)
  default     = {}
}

variable "seed_secret_placeholders" {
  description = <<-EOT
    Create a placeholder version for every secret so the first apply on a
    fresh project produces startable Cloud Run revisions.

    Set to false on any project whose secrets already hold real values: a
    newly created placeholder version becomes `latest` and would shadow them.
  EOT
  type        = bool
  default     = true
}
