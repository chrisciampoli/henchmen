variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "region" {
  description = "The GCP region (used to scope resource-level IAM conditions, e.g. Vertex AI publisher model paths)"
  type        = string
  default     = "us-central1"
}
