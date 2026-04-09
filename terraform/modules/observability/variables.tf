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

variable "labels" {
  description = "Labels to apply to observability resources"
  type        = map(string)
  default     = {}
}

variable "notification_channels" {
  description = "List of Cloud Monitoring notification channel resource names to attach to alert policies"
  type        = list(string)
  default     = []
}
