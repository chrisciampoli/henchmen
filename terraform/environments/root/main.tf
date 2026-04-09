provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  labels = {
    project     = "henchmen"
    environment = var.environment
    managed_by  = "terraform"
  }
}

# ---------------------------------------------------------------------------
# Foundation modules
# ---------------------------------------------------------------------------

module "bootstrap" {
  source = "../../modules/project-bootstrap"

  project_id = var.project_id
}

module "networking" {
  source = "../../modules/networking"

  project_id      = var.project_id
  region          = var.region
  environment     = var.environment
  labels          = local.labels
  allowlist_cidrs = var.allowlist_cidrs

  depends_on = [module.bootstrap]
}

module "iam" {
  source = "../../modules/iam"

  project_id  = var.project_id
  region      = var.region
  environment = var.environment

  depends_on = [module.bootstrap]
}

module "secrets" {
  source = "../../modules/secrets"

  project_id             = var.project_id
  environment            = var.environment
  labels                 = local.labels
  service_account_emails = module.iam.service_account_emails

  depends_on = [module.bootstrap]
}

# ---------------------------------------------------------------------------
# Service modules
# ---------------------------------------------------------------------------

module "data_stores" {
  source = "../../modules/data-stores"

  project_id  = var.project_id
  region      = var.region
  environment = var.environment

  depends_on = [module.bootstrap]
}

module "pubsub" {
  source = "../../modules/pubsub"

  project_id  = var.project_id
  environment = var.environment
  labels      = local.labels

  # Wire push endpoints from Cloud Run service URLs once they are known.
  push_endpoints = {
    mastermind_url = module.cloud_run_services.service_urls["mastermind"]
    dispatch_url   = module.cloud_run_services.service_urls["dispatch"]
    forge_url      = module.cloud_run_services.service_urls["forge"]
  }
  push_sa_email = module.iam.service_account_emails["mastermind"]

  depends_on = [module.bootstrap, module.cloud_run_services]
}

module "artifact_registry" {
  source = "../../modules/artifact-registry"

  project_id  = var.project_id
  region      = var.region
  environment = var.environment
  labels      = local.labels

  depends_on = [module.bootstrap]
}

# ---------------------------------------------------------------------------
# Deployment modules
# ---------------------------------------------------------------------------

module "cloud_run_services" {
  source = "../../modules/cloud-run-services"

  project_id             = var.project_id
  region                 = var.region
  environment            = var.environment
  labels                 = local.labels
  vpc_connector_id       = module.networking.connector_id
  service_account_emails = module.iam.service_account_emails
  github_default_repo    = var.github_default_repo
  container_image_tag    = var.container_image_tag
  # pubsub_push_sa_email: use the Mastermind SA which already holds roles/run.invoker
  pubsub_push_sa_email = module.iam.service_account_emails["mastermind"]

  depends_on = [module.bootstrap, module.networking, module.iam, module.artifact_registry]
}

module "cloud_run_lairs" {
  source = "../../modules/cloud-run-lairs"

  project_id         = var.project_id
  region             = var.region
  environment        = var.environment
  labels             = local.labels
  vpc_connector_id   = module.networking.connector_id
  operative_sa_email = module.iam.service_account_emails["operative"]
  operative_image    = "${module.artifact_registry.repository_url}/operative:${var.container_image_tag}"
  lair_cpu           = var.lair_cpu
  lair_memory        = var.lair_memory

  depends_on = [module.bootstrap, module.networking, module.iam, module.artifact_registry]
}

module "cloud_build" {
  source = "../../modules/cloud-build"

  project_id         = var.project_id
  region             = var.region
  environment        = var.environment
  labels             = local.labels
  github_owner       = var.github_owner
  github_repo        = var.github_repo
  enable_cloud_build = var.enable_cloud_build

  depends_on = [module.bootstrap]
}

module "observability" {
  source = "../../modules/observability"

  project_id  = var.project_id
  region      = var.region
  environment = var.environment
  labels      = local.labels
  # notification_channels left empty; add channel resource names here when configured

  depends_on = [module.bootstrap]
}

module "vertex_ai" {
  source = "../../modules/vertex-ai"

  project_id          = var.project_id
  region              = var.region
  environment         = var.environment
  labels              = local.labels
  mastermind_sa_email = module.iam.service_account_emails["mastermind"]

  depends_on = [module.bootstrap]
}

module "scheduler" {
  source = "../../modules/scheduler"
  count  = var.scheduler_enabled ? 1 : 0

  project_id     = var.project_id
  region         = var.region
  environment    = var.environment
  labels         = local.labels
  mastermind_url = module.cloud_run_services.service_urls["mastermind"]
  forge_url      = module.cloud_run_services.service_urls["forge"]
  # Use the Mastermind SA as the scheduler invoker; it already holds roles/run.invoker
  scheduler_sa_email = module.iam.service_account_emails["mastermind"]

  depends_on = [module.bootstrap, module.cloud_run_services]
}
