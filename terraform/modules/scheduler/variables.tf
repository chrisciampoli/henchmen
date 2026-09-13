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

variable "mastermind_url" {
  description = "The base URL of the Mastermind Cloud Run service"
  type        = string
}

variable "forge_url" {
  description = "The base URL of the Forge Cloud Run service"
  type        = string
}

variable "mastermind_audience" {
  description = "OIDC audience registered on the Mastermind service (custom_audiences), used for scheduler tokens"
  type        = string
}

variable "forge_audience" {
  description = "OIDC audience registered on the Forge service (custom_audiences), used for scheduler tokens"
  type        = string
}

variable "scheduler_sa_email" {
  description = "Service account email used by Cloud Scheduler to authenticate OIDC tokens"
  type        = string
}
