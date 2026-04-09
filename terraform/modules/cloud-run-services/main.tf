locals {
  # Resolve each component's image: use provided override or fall back to Artifact Registry default.
  default_registry = "${var.region}-docker.pkg.dev/${var.project_id}/henchmen-${var.environment}"

  images = {
    mastermind = lookup(var.container_images, "mastermind", "${local.default_registry}/mastermind:${var.container_image_tag}")
    arsenal    = lookup(var.container_images, "arsenal", "${local.default_registry}/arsenal:${var.container_image_tag}")
    dispatch   = lookup(var.container_images, "dispatch", "${local.default_registry}/dispatch:${var.container_image_tag}")
    forge      = lookup(var.container_images, "forge", "${local.default_registry}/forge:${var.container_image_tag}")
  }

  # Common environment variables injected into every container.
  common_env = [
    { name = "HENCHMEN_GCP_PROJECT_ID", value = var.project_id },
    { name = "HENCHMEN_ENVIRONMENT", value = var.environment },
    { name = "HENCHMEN_GCP_REGION", value = var.region },
    { name = "HENCHMEN_GITHUB_DEFAULT_REPO", value = var.github_default_repo },
  ]

  scaling = {
    min = var.environment == "prod" ? 1 : 0
    max = var.environment == "prod" ? 10 : 3
  }
}

# ---------------------------------------------------------------------------
# Mastermind
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "mastermind" {
  name     = "henchmen-${var.environment}-mastermind"
  location = var.region
  project  = var.project_id

  # Prevent terraform apply from stripping secret env vars set via gcloud/console.
  lifecycle {
    ignore_changes = [template[0].containers[0].env]
  }

  template {
    timeout = "3600s"
    service_account = var.service_account_emails["mastermind"]

    vpc_access {
      connector = var.vpc_connector_id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = local.images["mastermind"]

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      env {
        name = "SLACK_BOT_TOKEN"
        value_source { secret_key_ref { secret = "henchmen-${var.environment}-slack-bot-token"; version = "1" } }
      }
      env {
        name = "GITHUB_TOKEN"
        value_source { secret_key_ref { secret = "henchmen-${var.environment}-github-token"; version = "latest" } }
      }
      # PINECONE_API_KEY removed — migrated to Vector Search 2.0 (GCP-native auth)

      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
      }
    }

    scaling {
      min_instance_count = local.scaling.min
      max_instance_count = local.scaling.max
    }
  }

  labels = var.labels
}

# ---------------------------------------------------------------------------
# Arsenal
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "arsenal" {
  name     = "henchmen-${var.environment}-arsenal"
  location = var.region
  project  = var.project_id

  # Prevent terraform apply from stripping secret env vars set via gcloud/console.
  lifecycle {
    ignore_changes = [template[0].containers[0].env]
  }

  template {
    service_account = var.service_account_emails["arsenal"]

    vpc_access {
      connector = var.vpc_connector_id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = local.images["arsenal"]

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }

    scaling {
      min_instance_count = local.scaling.min
      max_instance_count = local.scaling.max
    }
  }

  labels = var.labels
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "dispatch" {
  name     = "henchmen-${var.environment}-dispatch"
  location = var.region
  project  = var.project_id

  # Prevent terraform apply from stripping secret env vars set via gcloud/console.
  lifecycle {
    ignore_changes = [template[0].containers[0].env]
  }

  template {
    service_account = var.service_account_emails["dispatch"]

    vpc_access {
      connector = var.vpc_connector_id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = local.images["dispatch"]

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      env {
        name = "SLACK_BOT_TOKEN"
        value_source { secret_key_ref { secret = "henchmen-${var.environment}-slack-bot-token"; version = "1" } }
      }
      env {
        name = "SLACK_SIGNING_SECRET"
        value_source { secret_key_ref { secret = "henchmen-${var.environment}-slack-signing-secret"; version = "1" } }
      }
      env {
        name = "SLACK_APP_TOKEN"
        value_source { secret_key_ref { secret = "henchmen-${var.environment}-slack-app-token"; version = "1" } }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }

    scaling {
      min_instance_count = local.scaling.min
      max_instance_count = local.scaling.max
    }
  }

  labels = var.labels
}

# ---------------------------------------------------------------------------
# Forge
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "forge" {
  name     = "henchmen-${var.environment}-forge"
  location = var.region
  project  = var.project_id

  # Prevent terraform apply from stripping secret env vars set via gcloud/console.
  lifecycle {
    ignore_changes = [template[0].containers[0].env]
  }

  template {
    service_account = var.service_account_emails["forge"]

    vpc_access {
      connector = var.vpc_connector_id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = local.images["forge"]

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      env {
        name = "GITHUB_TOKEN"
        value_source { secret_key_ref { secret = "henchmen-${var.environment}-github-token"; version = "latest" } }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }

    scaling {
      min_instance_count = local.scaling.min
      max_instance_count = local.scaling.max
    }
  }

  labels = var.labels
}

# ---------------------------------------------------------------------------
# IAM: allow Pub/Sub push SA to invoke each service
# ---------------------------------------------------------------------------

locals {
  pubsub_invoker_services = {
    mastermind = google_cloud_run_v2_service.mastermind.name
    arsenal    = google_cloud_run_v2_service.arsenal.name
    dispatch   = google_cloud_run_v2_service.dispatch.name
    forge      = google_cloud_run_v2_service.forge.name
  }
}

resource "google_cloud_run_v2_service_iam_member" "pubsub_invoker" {
  for_each = local.pubsub_invoker_services

  project  = var.project_id
  location = var.region
  name     = each.value
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.pubsub_push_sa_email}"
}
