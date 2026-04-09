variable "project_id" {
  description = "The GCP project ID"
  type        = string
}

variable "environment" {
  description = "The deployment environment (e.g. dev, staging, prod)"
  type        = string
}

variable "labels" {
  description = "Labels to apply to Pub/Sub resources"
  type        = map(string)
  default     = {}
}

variable "push_endpoints" {
  description = "Service URLs for push subscriptions"
  type = object({
    mastermind_url = string
    dispatch_url   = string
    forge_url      = string
  })
  default = {
    mastermind_url = "https://mastermind-placeholder.example.com"
    dispatch_url   = "https://dispatch-placeholder.example.com"
    forge_url      = "https://forge-placeholder.example.com"
  }
}

variable "push_sa_email" {
  description = "Service account email for Pub/Sub push OIDC authentication"
  type        = string
  default     = ""
}
