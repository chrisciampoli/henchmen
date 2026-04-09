variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region for Cloud Scheduler jobs"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "labels" {
  description = "Labels to apply to Cloud Scheduler jobs"
  type        = map(string)
  default     = {}
}

variable "mastermind_url" {
  description = "The base URL of the Mastermind Cloud Run service"
  type        = string
}

variable "forge_url" {
  description = "The base URL of the Forge Cloud Run service"
  type        = string
}

variable "scheduler_sa_email" {
  description = "Service account email used by Cloud Scheduler to authenticate OIDC tokens"
  type        = string
}
