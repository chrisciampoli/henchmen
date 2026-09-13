variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region for regional observability resources"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "notification_channels" {
  description = "List of Cloud Monitoring notification channel resource names to attach to alert policies"
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  description = "Retention, in days, for the Henchmen log bucket"
  type        = number
  default     = 30

  validation {
    condition     = var.log_retention_days > 0
    error_message = "log_retention_days must be greater than 0."
  }
}
