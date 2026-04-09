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
  description = "Map of component name to service account email (from iam module)"
  type        = map(string)
  # Expected keys: mastermind, arsenal, dispatch, forge
}

variable "container_images" {
  description = "Map of component name to container image URL. Defaults to Artifact Registry paths."
  type        = map(string)
  default     = {}
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

variable "container_image_tag" {
  description = "Container image tag to deploy (e.g. a git short SHA or 'latest')"
  type        = string
  default     = "latest"
}
