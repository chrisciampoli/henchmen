variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "service_account_emails" {
  description = "Map of service name to service account email (from the iam module outputs)"
  type        = map(string)
  # Expected keys: mastermind, dispatch, operative, arsenal, forge, dossier
}

variable "labels" {
  description = "Labels to apply to Secret Manager secrets"
  type        = map(string)
  default     = {}
}
