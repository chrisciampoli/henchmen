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
}

variable "push_audiences" {
  description = <<-EOT
    OIDC audience each push subscription signs, per service.

    Must be the value the receiving service registers via custom_audiences and
    verifies as HENCHMEN_PUBSUB_OIDC_AUDIENCE (the cloud-run-services module
    exports both from one local), otherwise every push is rejected with 401.
  EOT
  type = object({
    mastermind = string
    dispatch   = string
    forge      = string
  })
}

variable "push_sa_email" {
  description = "Service account email for Pub/Sub push OIDC authentication"
  type        = string
}

variable "enable_cloud_build_notifications" {
  description = "Create a push subscription from the project's `cloud-builds` topic to Forge. Requires that topic to exist (Cloud Build creates it on the first build)."
  type        = bool
  default     = false
}
