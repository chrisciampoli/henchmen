# The actual module composition lives in ../root. This file is just a thin
# wrapper that points the root module at the dev environment. Per-environment
# values (min_instances, lair sizing, allowlists, etc.) live in dev.auto.tfvars.
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

  # Per-environment overrides (values in dev.auto.tfvars).
  lair_cpu           = var.lair_cpu
  lair_memory        = var.lair_memory
  allowlist_cidrs    = var.allowlist_cidrs
  scheduler_enabled  = var.scheduler_enabled
  enable_cloud_build = var.enable_cloud_build
}
