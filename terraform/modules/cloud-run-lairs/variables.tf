variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region to deploy the Cloud Run Job template into"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "labels" {
  description = "Labels to apply to Cloud Run Job resources"
  type        = map(string)
  default     = {}
}

variable "vpc_connector_id" {
  description = "The ID of the VPC Serverless Access Connector (from networking module)"
  type        = string
}

variable "operative_sa_email" {
  description = "Service account email for Operative Cloud Run Jobs (from iam module)"
  type        = string
}

variable "operative_image" {
  description = "Container image URL for the Operative runtime"
  type        = string
}

variable "lair_cpu" {
  description = "CPU limit for each Lair container (vCPU)"
  type        = string
  default     = "4"
}

variable "lair_memory" {
  description = "Memory limit for each Lair container"
  type        = string
  default     = "8Gi"
}

variable "lair_timeout" {
  description = "Maximum execution duration for a Lair job, in seconds"
  type        = number
  default     = 1800
}
