variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region for Vertex AI resources"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "labels" {
  description = "Labels to apply to Vertex AI resources"
  type        = map(string)
  default     = {}
}

variable "mastermind_sa_email" {
  description = "Service account email for Mastermind (used for Agent Engine access)"
  type        = string
}
