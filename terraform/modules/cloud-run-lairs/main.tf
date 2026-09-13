# ---------------------------------------------------------------------------
# Operative Lair template
#
# Reference definition of an Operative Lair: the image, service account,
# sizing, secret mounts and environment a lair runs with. Mastermind does NOT
# clone this job at runtime — it creates a fresh `lair-<task>-<node>` job per
# node through the Cloud Run Jobs API — so the sizing and image that actually
# take effect are the HENCHMEN_LAIR_* values injected into the Mastermind
# service (see the cloud-run-services module), which this module is kept in
# sync with. The template exists so the rendered job spec is reviewable in
# terraform and so `gcloud run jobs execute` can be used to smoke-test the
# operative image by hand.
# ---------------------------------------------------------------------------

locals {
  lair_env = {
    HENCHMEN_GCP_PROJECT_ID       = var.project_id
    HENCHMEN_ENVIRONMENT          = var.environment
    HENCHMEN_GCP_REGION           = var.region
    HENCHMEN_FIRESTORE_DATABASE   = var.firestore_database
    HENCHMEN_GCS_BUCKET_DOSSIER   = var.gcs_bucket_dossier
    HENCHMEN_GCS_BUCKET_SNAPSHOTS = var.gcs_bucket_snapshots
  }
}

resource "google_cloud_run_v2_job" "lair_template" {
  name     = "henchmen-${var.environment}-lair-template"
  location = var.region
  project  = var.project_id

  template {
    template {
      service_account = var.operative_sa_email

      vpc_access {
        connector = var.vpc_connector_id
        egress    = "PRIVATE_RANGES_ONLY"
      }

      containers {
        image = var.operative_image

        dynamic "env" {
          for_each = { for k, v in local.lair_env : k => v if v != "" }
          content {
            name  = env.key
            value = env.value
          }
        }

        # The operative clones and pushes with this token. Settings accepts the
        # bare name as an alias for HENCHMEN_GITHUB_TOKEN.
        env {
          name = "GITHUB_TOKEN"
          value_source {
            secret_key_ref {
              secret  = "henchmen-${var.environment}-github-token"
              version = "latest"
            }
          }
        }

        resources {
          limits = {
            cpu    = var.lair_cpu
            memory = var.lair_memory
          }
        }
      }

      timeout     = "${var.lair_timeout}s"
      max_retries = 0
    }
  }

  labels = var.labels
}
