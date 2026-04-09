variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "region" {
  description = "The GCP region to deploy networking resources into"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "subnet_cidr" {
  description = "The IP CIDR range for the primary subnet"
  type        = string
  default     = "10.0.0.0/20"
}

variable "labels" {
  description = "Labels to apply to networking resources"
  type        = map(string)
  default     = {}
}

variable "allowlist_cidrs" {
  description = "Additional egress CIDR ranges to allow (e.g. Slack, Jira/Atlassian)"
  type        = list(string)
  default     = []
}
