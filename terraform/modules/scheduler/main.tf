# ---------------------------------------------------------------------------
# Cloud Scheduler — periodic maintenance jobs
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "stale_task_cleanup" {
  project   = var.project_id
  region    = var.region
  name      = "henchmen-${var.environment}-stale-task-cleanup"
  schedule  = "0 */6 * * *" # Every 6 hours
  time_zone = "UTC"

  http_target {
    uri         = "${var.mastermind_url}/api/v1/cleanup"
    http_method = "POST"

    oidc_token {
      service_account_email = var.scheduler_sa_email
      audience              = var.mastermind_url
    }
  }

  retry_config {
    retry_count = 3
  }
}

resource "google_cloud_scheduler_job" "merge_queue_processor" {
  project   = var.project_id
  region    = var.region
  name      = "henchmen-${var.environment}-merge-queue-processor"
  schedule  = "*/5 * * * *" # Every 5 minutes
  time_zone = "UTC"

  http_target {
    uri         = "${var.forge_url}/api/v1/process-queue"
    http_method = "POST"

    oidc_token {
      service_account_email = var.scheduler_sa_email
      audience              = var.forge_url
    }
  }

  retry_config {
    retry_count = 3
  }
}

# ---------------------------------------------------------------------------
# Watchdog — detect stalled tasks and trigger recovery
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "watchdog" {
  project          = var.project_id
  name             = "henchmen-${var.environment}-watchdog"
  region           = var.region
  schedule         = "*/5 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "60s"

  http_target {
    http_method = "POST"
    uri         = "${var.mastermind_url}/api/v1/watchdog"

    oidc_token {
      service_account_email = var.scheduler_sa_email
      audience              = var.mastermind_url
    }
  }

  retry_config {
    retry_count = 3
  }
}

# ---------------------------------------------------------------------------
# DLQ monitor — check dead letter queue for lost messages
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "dlq_check" {
  project          = var.project_id
  name             = "henchmen-${var.environment}-dlq-check"
  region           = var.region
  schedule         = "*/15 * * * *"
  time_zone        = "UTC"
  attempt_deadline = "60s"

  http_target {
    http_method = "POST"
    uri         = "${var.mastermind_url}/api/v1/check-dlq"

    oidc_token {
      service_account_email = var.scheduler_sa_email
      audience              = var.mastermind_url
    }
  }

  retry_config {
    retry_count = 3
  }
}
