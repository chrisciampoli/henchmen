variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region for Firestore and Cloud Storage resources"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "labels" {
  description = "Labels to apply to storage buckets"
  type        = map(string)
  default     = {}
}

variable "service_account_emails" {
  description = "Map of service name to service account email (from the iam module). Required keys: mastermind, operative, forge."
  type        = map(string)

  validation {
    condition     = length(setsubtract(["mastermind", "operative", "forge"], keys(var.service_account_emails))) == 0
    error_message = "service_account_emails must contain keys: mastermind, operative, forge."
  }
}

variable "artifact_retention_days" {
  description = "Days after which dossier artifacts and operative snapshots are deleted"
  type        = number
  default     = 90

  validation {
    condition     = var.artifact_retention_days > 0
    error_message = "artifact_retention_days must be greater than 0."
  }
}
