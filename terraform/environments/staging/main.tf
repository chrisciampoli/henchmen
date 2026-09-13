# The actual module composition lives in ../root. This file is just a thin
# wrapper that points the root module at the staging environment. Per-environment
# values (lair sizing, scheduler, retention, etc.) live in staging.auto.tfvars;
# the identity values that cannot be committed (project_id, github_owner,
# github_default_repo) go in terraform.tfvars — copy
# staging.auto.tfvars.example to get started.
#
# See ./README.md for the init/apply workflow.

terraform {
  required_version = ">= 1.7"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

module "henchmen" {
  source = "../root"

  project_id          = var.project_id
  region              = var.region
  environment         = var.environment
  github_owner        = var.github_owner
  github_repo         = var.github_repo
  github_default_repo = var.github_default_repo
  container_image_tag = var.container_image_tag
  container_images    = var.container_images

  # Intake configuration.
  jira_base_url           = var.jira_base_url
  jira_email              = var.jira_email
  dispatch_public_ingress = var.dispatch_public_ingress
  internal_only_ingress   = var.internal_only_ingress

  # Per-environment overrides (values in staging.auto.tfvars).
  lair_cpu                         = var.lair_cpu
  lair_memory                      = var.lair_memory
  lair_timeout                     = var.lair_timeout
  scheduler_enabled                = var.scheduler_enabled
  enable_cloud_build               = var.enable_cloud_build
  enable_cloud_build_notifications = var.enable_cloud_build_notifications
  seed_secret_placeholders         = var.seed_secret_placeholders
  log_retention_days               = var.log_retention_days
  artifact_retention_days          = var.artifact_retention_days
}
