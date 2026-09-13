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

  project_id  = var.project_id
  region      = var.region
  environment = var.environment

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

  project_id               = var.project_id
  environment              = var.environment
  labels                   = local.labels
  service_account_emails   = module.iam.service_account_emails
  seed_secret_placeholders = var.seed_secret_placeholders

  depends_on = [module.bootstrap]
}

# ---------------------------------------------------------------------------
# Service modules
# ---------------------------------------------------------------------------

module "data_stores" {
  source = "../../modules/data-stores"

  project_id              = var.project_id
  region                  = var.region
  environment             = var.environment
  labels                  = local.labels
  service_account_emails  = module.iam.service_account_emails
  artifact_retention_days = var.artifact_retention_days

  depends_on = [module.bootstrap, module.iam]
}

module "pubsub" {
  source = "../../modules/pubsub"

  project_id  = var.project_id
  environment = var.environment
  labels      = local.labels

  # Push endpoints come from the Cloud Run service URLs; the audience is the
  # fixed string each service registers via custom_audiences and verifies as
  # HENCHMEN_PUBSUB_OIDC_AUDIENCE, so subscription and application agree by
  # construction.
  push_endpoints = {
    mastermind_url = module.cloud_run_services.service_urls["mastermind"]
    dispatch_url   = module.cloud_run_services.service_urls["dispatch"]
    forge_url      = module.cloud_run_services.service_urls["forge"]
  }
  push_audiences = {
    mastermind = module.cloud_run_services.pubsub_audiences["mastermind"]
    dispatch   = module.cloud_run_services.pubsub_audiences["dispatch"]
    forge      = module.cloud_run_services.pubsub_audiences["forge"]
  }
  push_sa_email                    = module.iam.service_account_emails["pubsub_push"]
  enable_cloud_build_notifications = var.enable_cloud_build_notifications

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
  container_images       = var.container_images
  firestore_database     = module.data_stores.database_name
  gcs_bucket_dossier     = module.data_stores.dossier_bucket_name
  gcs_bucket_snapshots   = module.data_stores.snapshots_bucket_name
  jira_base_url          = var.jira_base_url
  jira_email             = var.jira_email

  dispatch_public_ingress = var.dispatch_public_ingress
  internal_only_ingress   = var.internal_only_ingress

  # Sizing for the lair jobs Mastermind creates at runtime. The lair template
  # job below is rendered from the same values.
  lair_cpu     = var.lair_cpu
  lair_memory  = var.lair_memory
  lair_timeout = var.lair_timeout

  pubsub_push_sa_email = module.iam.service_account_emails["pubsub_push"]
  scheduler_sa_email   = module.iam.service_account_emails["scheduler"]

  depends_on = [module.bootstrap, module.networking, module.iam, module.secrets, module.data_stores, module.artifact_registry]
}

module "cloud_run_lairs" {
  source = "../../modules/cloud-run-lairs"

  project_id           = var.project_id
  region               = var.region
  environment          = var.environment
  labels               = local.labels
  vpc_connector_id     = module.networking.connector_id
  operative_sa_email   = module.iam.service_account_emails["operative"]
  operative_image      = var.container_image_tag == "" ? var.placeholder_image : "${module.artifact_registry.repository_url}/operative:${var.container_image_tag}"
  firestore_database   = module.data_stores.database_name
  gcs_bucket_dossier   = module.data_stores.dossier_bucket_name
  gcs_bucket_snapshots = module.data_stores.snapshots_bucket_name
  lair_cpu             = var.lair_cpu
  lair_memory          = var.lair_memory
  lair_timeout         = var.lair_timeout

  depends_on = [module.bootstrap, module.networking, module.iam, module.secrets, module.data_stores, module.artifact_registry]
}

module "cloud_build" {
  source = "../../modules/cloud-build"

  project_id         = var.project_id
  region             = var.region
  environment        = var.environment
  github_owner       = var.github_owner
  github_repo        = var.github_repo
  enable_cloud_build = var.enable_cloud_build

  depends_on = [module.bootstrap]
}

module "observability" {
  source = "../../modules/observability"

  project_id         = var.project_id
  region             = var.region
  environment        = var.environment
  log_retention_days = var.log_retention_days
  # notification_channels left empty; add channel resource names here when configured

  depends_on = [module.bootstrap]
}

module "scheduler" {
  source = "../../modules/scheduler"
  count  = var.scheduler_enabled ? 1 : 0

  project_id          = var.project_id
  region              = var.region
  environment         = var.environment
  mastermind_url      = module.cloud_run_services.service_urls["mastermind"]
  forge_url           = module.cloud_run_services.service_urls["forge"]
  mastermind_audience = module.cloud_run_services.pubsub_audiences["mastermind"]
  forge_audience      = module.cloud_run_services.pubsub_audiences["forge"]
  scheduler_sa_email  = module.iam.service_account_emails["scheduler"]

  depends_on = [module.bootstrap, module.cloud_run_services]
}
