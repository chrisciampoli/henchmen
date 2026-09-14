# ---------------------------------------------------------------------------
# Cloud Run services: Mastermind, Dispatch, Forge.
#
# Arsenal is NOT a service — it is the tool registry that runs in-process
# inside the Operative container, and no arsenal image is ever built.
# ---------------------------------------------------------------------------

locals {
  services = ["mastermind", "dispatch", "forge"]

  # Resolve each component's image: use the provided override, otherwise the
  # Artifact Registry path for the requested tag. When container_image_tag is
  # empty (the default, i.e. "images have not been pushed yet") every service
  # falls back to a public placeholder image so that the *first* terraform
  # apply on a fresh project converges instead of failing on an unpullable
  # image. Set container_image_tag once the images exist and re-apply.
  default_registry = "${var.region}-docker.pkg.dev/${var.project_id}/henchmen-${var.environment}"

  images = {
    for svc in local.services :
    svc => lookup(
      var.container_images,
      svc,
      var.container_image_tag == "" ? var.placeholder_image : "${local.default_registry}/${svc}:${var.container_image_tag}"
    )
  }

  # Stable, self-chosen OIDC audiences. Using a fixed string (registered on the
  # service via custom_audiences) instead of the service URL keeps the value
  # the Pub/Sub subscription signs identical to the value the application
  # verifies, without a terraform cycle on the not-yet-known service URI.
  pubsub_audiences = {
    for svc in local.services : svc => "henchmen-${var.environment}-${svc}"
  }

  # Common environment variables injected into every container. Empty values
  # are dropped so the application defaults apply.
  common_env = {
    HENCHMEN_GCP_PROJECT_ID             = var.project_id
    HENCHMEN_ENVIRONMENT                = var.environment
    HENCHMEN_GCP_REGION                 = var.region
    HENCHMEN_GITHUB_DEFAULT_REPO        = var.github_default_repo
    HENCHMEN_FIRESTORE_DATABASE         = var.firestore_database
    HENCHMEN_GCS_BUCKET_DOSSIER         = var.gcs_bucket_dossier
    HENCHMEN_GCS_BUCKET_SNAPSHOTS       = var.gcs_bucket_snapshots
    HENCHMEN_PUBSUB_OIDC_ALLOWED_EMAILS = var.pubsub_push_sa_email
  }

  # Per-service plain environment, merged over common_env.
  service_env = {
    mastermind = merge(local.common_env, {
      HENCHMEN_PUBSUB_OIDC_AUDIENCE     = local.pubsub_audiences["mastermind"]
      HENCHMEN_LAIR_DEFAULT_CPU         = var.lair_cpu
      HENCHMEN_LAIR_DEFAULT_MEMORY      = var.lair_memory
      HENCHMEN_LAIR_DEFAULT_TIMEOUT     = tostring(var.lair_timeout)
      HENCHMEN_LAIR_OPERATIVE_IMAGE_TAG = var.container_image_tag == "" ? "latest" : var.container_image_tag
      HENCHMEN_LAIR_SERVICE_ACCOUNT     = var.service_account_emails["operative"]
    })
    dispatch = merge(local.common_env, {
      HENCHMEN_PUBSUB_OIDC_AUDIENCE = local.pubsub_audiences["dispatch"]
      JIRA_SERVER                   = var.jira_base_url
      JIRA_EMAIL                    = var.jira_email
    })
    forge = merge(local.common_env, {
      HENCHMEN_PUBSUB_OIDC_AUDIENCE = local.pubsub_audiences["forge"]
    })
  }

  # Secret Manager mounts, keyed by env var name -> secret suffix. The bare
  # names (GITHUB_TOKEN, SLACK_*, JIRA_API_TOKEN) are the ones Settings accepts
  # as aliases for the corresponding HENCHMEN_* fields.
  secret_env = {
    mastermind = {
      GITHUB_TOKEN                = "github-token"
      SLACK_BOT_TOKEN             = "slack-bot-token"
      HENCHMEN_METRICS_AUTH_TOKEN = "metrics-auth-token"
    }
    dispatch = {
      SLACK_BOT_TOKEN             = "slack-bot-token"
      SLACK_SIGNING_SECRET        = "slack-signing-secret"
      SLACK_APP_TOKEN             = "slack-app-token"
      JIRA_API_TOKEN              = "jira-api-token"
      HENCHMEN_METRICS_AUTH_TOKEN = "metrics-auth-token"
    }
    forge = {
      GITHUB_TOKEN                = "github-token"
      HENCHMEN_METRICS_AUTH_TOKEN = "metrics-auth-token"
    }
  }

  scaling = {
    min = var.environment == "prod" ? 1 : 0
    max = var.environment == "prod" ? 10 : 3
  }

  internal_ingress = var.internal_only_ingress ? "INGRESS_TRAFFIC_INTERNAL_ONLY" : "INGRESS_TRAFFIC_ALL"
}

# ---------------------------------------------------------------------------
# Mastermind
#
# Receives only Pub/Sub push and Cloud Scheduler traffic, both of which count
# as internal ingress, so the public internet never needs to reach it. Set
# internal_only_ingress = false to fall back to open ingress (IAM still
# requires an invoker binding) if a deployment fronts these with a load
# balancer or calls them from outside the project.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "mastermind" {
  name             = "henchmen-${var.environment}-mastermind"
  location         = var.region
  project          = var.project_id
  ingress          = local.internal_ingress
  custom_audiences = [local.pubsub_audiences["mastermind"]]

  template {
    timeout         = "3600s"
    service_account = var.service_account_emails["mastermind"]

    vpc_access {
      connector = var.vpc_connector_id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = local.images["mastermind"]

      dynamic "env" {
        for_each = { for k, v in local.service_env["mastermind"] : k => v if v != "" }
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env["mastermind"]
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = "henchmen-${var.environment}-${env.value}"
              version = "latest"
            }
          }
        }
      }

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
# Dispatch
#
# The only service that must accept unauthenticated third-party traffic
# (GitHub / Jira webhooks), which is why ingress stays open. Invocation is
# still denied by IAM unless var.dispatch_public_ingress is set — see the
# allUsers binding at the bottom of this file.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "dispatch" {
  name             = "henchmen-${var.environment}-dispatch"
  location         = var.region
  project          = var.project_id
  ingress          = "INGRESS_TRAFFIC_ALL"
  custom_audiences = [local.pubsub_audiences["dispatch"]]

  template {
    service_account = var.service_account_emails["dispatch"]

    vpc_access {
      connector = var.vpc_connector_id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = local.images["dispatch"]

      dynamic "env" {
        for_each = { for k, v in local.service_env["dispatch"] : k => v if v != "" }
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env["dispatch"]
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = "henchmen-${var.environment}-${env.value}"
              version = "latest"
            }
          }
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
# Forge
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "forge" {
  name             = "henchmen-${var.environment}-forge"
  location         = var.region
  project          = var.project_id
  ingress          = local.internal_ingress
  custom_audiences = [local.pubsub_audiences["forge"]]

  template {
    # A Forge CI run is capped at forge_ci_timeout_seconds (default 540s) so it
    # finishes inside Pub/Sub's 600s ack deadline. Cloud Run's 300s default
    # request timeout would kill the request long before that budget runs out.
    timeout         = "600s"
    service_account = var.service_account_emails["forge"]

    vpc_access {
      connector = var.vpc_connector_id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = local.images["forge"]

      dynamic "env" {
        for_each = { for k, v in local.service_env["forge"] : k => v if v != "" }
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.secret_env["forge"]
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = "henchmen-${var.environment}-${env.value}"
              version = "latest"
            }
          }
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
# IAM: who may invoke each service
# ---------------------------------------------------------------------------

locals {
  service_names = {
    mastermind = google_cloud_run_v2_service.mastermind.name
    dispatch   = google_cloud_run_v2_service.dispatch.name
    forge      = google_cloud_run_v2_service.forge.name
  }

  # Cloud Scheduler only calls Mastermind (cleanup, watchdog, DLQ check) and
  # Forge (merge queue processor).
  scheduler_target_services = {
    mastermind = google_cloud_run_v2_service.mastermind.name
    forge      = google_cloud_run_v2_service.forge.name
  }
}

resource "google_cloud_run_v2_service_iam_member" "pubsub_invoker" {
  for_each = local.service_names

  project  = var.project_id
  location = var.region
  name     = each.value
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.pubsub_push_sa_email}"
}

resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  for_each = local.scheduler_target_services

  project  = var.project_id
  location = var.region
  name     = each.value
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.scheduler_sa_email}"
}

# Opt-in public invocation for Dispatch. GitHub and Jira cannot present a
# Google OIDC token, so without this their webhook deliveries are rejected at
# the Cloud Run edge with 403 before Dispatch's own signature verification
# runs. Leave it false unless those intake sources are in use — /api/v1/tasks
# is on the same service.
resource "google_cloud_run_v2_service_iam_member" "dispatch_public_invoker" {
  count = var.dispatch_public_ingress ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.dispatch.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
