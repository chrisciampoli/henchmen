# ---------------------------------------------------------------------------
# Operative Lair template
#
# This Cloud Run Job serves as the canonical template that Mastermind clones
# (via the Jobs API) whenever it needs to spawn a new Lair for an operative.
# The job itself is not meant to be executed directly; Mastermind creates
# per-task job executions with overridden environment variables.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "lair_template" {
  name     = "henchmen-${var.environment}-lair-template"
  location = var.region
  project  = var.project_id

  # Prevent terraform apply from stripping secret env vars set via gcloud/console.
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].env]
  }

  template {
    template {
      service_account = var.operative_sa_email

      vpc_access {
        connector = var.vpc_connector_id
        egress    = "ALL_TRAFFIC"
      }

      containers {
        image = var.operative_image

        env {
          name  = "HENCHMEN_GCP_PROJECT_ID"
          value = var.project_id
        }
        env {
          name  = "HENCHMEN_ENVIRONMENT"
          value = var.environment
        }
        env {
          name  = "HENCHMEN_GCP_REGION"
          value = var.region
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
