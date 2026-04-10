locals {
  # Common subscription settings applied to every subscription
  retention_duration       = "604800s" # 7 days
  ack_deadline_seconds     = 600
  dead_letter_max_attempts = 5
  retry_min_backoff        = "10s"
  retry_max_backoff        = "600s"
}

# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

resource "google_pubsub_topic" "task_intake" {
  project = var.project_id
  name    = "henchmen-${var.environment}-task-intake"
  labels  = var.labels
}

resource "google_pubsub_topic" "task_planned" {
  project = var.project_id
  name    = "henchmen-${var.environment}-task-planned"
  labels  = var.labels
}

resource "google_pubsub_topic" "operative_dispatch" {
  project = var.project_id
  name    = "henchmen-${var.environment}-operative-dispatch"
  labels  = var.labels
}

resource "google_pubsub_topic" "operative_status" {
  project = var.project_id
  name    = "henchmen-${var.environment}-operative-status"
  labels  = var.labels
}

resource "google_pubsub_topic" "operative_complete" {
  project = var.project_id
  name    = "henchmen-${var.environment}-operative-complete"
  labels  = var.labels
}

resource "google_pubsub_topic" "forge_request" {
  project = var.project_id
  name    = "henchmen-${var.environment}-forge-request"
  labels  = var.labels
}

resource "google_pubsub_topic" "forge_result" {
  project = var.project_id
  name    = "henchmen-${var.environment}-forge-result"
  labels  = var.labels
}

resource "google_pubsub_topic" "dead_letter" {
  project = var.project_id
  name    = "henchmen-${var.environment}-dead-letter"
  labels  = var.labels
}

# ---------------------------------------------------------------------------
# Push subscriptions
# ---------------------------------------------------------------------------

# henchmen-task-intake → Mastermind
resource "google_pubsub_subscription" "task_intake" {
  project = var.project_id
  name    = "henchmen-${var.environment}-task-intake-sub"
  topic   = google_pubsub_topic.task_intake.name

  message_retention_duration = local.retention_duration
  ack_deadline_seconds       = local.ack_deadline_seconds

  push_config {
    push_endpoint = "${var.push_endpoints.mastermind_url}/pubsub/task-intake"
    oidc_token {
      service_account_email = var.push_sa_email
      audience              = var.push_endpoints.mastermind_url
    }
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = local.dead_letter_max_attempts
  }

  retry_policy {
    minimum_backoff = local.retry_min_backoff
    maximum_backoff = local.retry_max_backoff
  }
}

# henchmen-task-planned → Dispatch (status updates)
resource "google_pubsub_subscription" "task_planned" {
  project = var.project_id
  name    = "henchmen-${var.environment}-task-planned-sub"
  topic   = google_pubsub_topic.task_planned.name

  message_retention_duration = local.retention_duration
  ack_deadline_seconds       = local.ack_deadline_seconds

  push_config {
    push_endpoint = "${var.push_endpoints.dispatch_url}/pubsub/task-planned"
    oidc_token {
      service_account_email = var.push_sa_email
      audience              = var.push_endpoints.dispatch_url
    }
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = local.dead_letter_max_attempts
  }

  retry_policy {
    minimum_backoff = local.retry_min_backoff
    maximum_backoff = local.retry_max_backoff
  }
}

# henchmen-operative-dispatch → pull (Lair launcher pulls work)
resource "google_pubsub_subscription" "operative_dispatch" {
  project = var.project_id
  name    = "henchmen-${var.environment}-operative-dispatch-sub"
  topic   = google_pubsub_topic.operative_dispatch.name

  message_retention_duration   = local.retention_duration
  ack_deadline_seconds         = local.ack_deadline_seconds
  enable_exactly_once_delivery = true

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = local.dead_letter_max_attempts
  }

  retry_policy {
    minimum_backoff = local.retry_min_backoff
    maximum_backoff = local.retry_max_backoff
  }
}

# henchmen-operative-status → pull (informational, no active handler)
resource "google_pubsub_subscription" "operative_status" {
  project = var.project_id
  name    = "henchmen-${var.environment}-operative-status-sub"
  topic   = google_pubsub_topic.operative_status.name

  message_retention_duration = local.retention_duration
  ack_deadline_seconds       = local.ack_deadline_seconds

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = local.dead_letter_max_attempts
  }

  retry_policy {
    minimum_backoff = local.retry_min_backoff
    maximum_backoff = local.retry_max_backoff
  }
}

# henchmen-operative-complete → Mastermind
resource "google_pubsub_subscription" "operative_complete" {
  project = var.project_id
  name    = "henchmen-${var.environment}-operative-complete-sub"
  topic   = google_pubsub_topic.operative_complete.name

  message_retention_duration = local.retention_duration
  ack_deadline_seconds       = local.ack_deadline_seconds

  push_config {
    push_endpoint = "${var.push_endpoints.mastermind_url}/pubsub/operative-complete"
    oidc_token {
      service_account_email = var.push_sa_email
      audience              = var.push_endpoints.mastermind_url
    }
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = local.dead_letter_max_attempts
  }

  retry_policy {
    minimum_backoff = local.retry_min_backoff
    maximum_backoff = local.retry_max_backoff
  }
}

# henchmen-forge-request → Forge
resource "google_pubsub_subscription" "forge_request" {
  project = var.project_id
  name    = "henchmen-${var.environment}-forge-request-sub"
  topic   = google_pubsub_topic.forge_request.name

  message_retention_duration = local.retention_duration
  ack_deadline_seconds       = local.ack_deadline_seconds

  push_config {
    push_endpoint = "${var.push_endpoints.forge_url}/pubsub/forge-request"
    oidc_token {
      service_account_email = var.push_sa_email
      audience              = var.push_endpoints.forge_url
    }
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = local.dead_letter_max_attempts
  }

  retry_policy {
    minimum_backoff = local.retry_min_backoff
    maximum_backoff = local.retry_max_backoff
  }
}

# henchmen-forge-result → Mastermind
resource "google_pubsub_subscription" "forge_result" {
  project = var.project_id
  name    = "henchmen-${var.environment}-forge-result-sub"
  topic   = google_pubsub_topic.forge_result.name

  message_retention_duration = local.retention_duration
  ack_deadline_seconds       = local.ack_deadline_seconds

  push_config {
    push_endpoint = "${var.push_endpoints.mastermind_url}/pubsub/forge-result"
    oidc_token {
      service_account_email = var.push_sa_email
      audience              = var.push_endpoints.mastermind_url
    }
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = local.dead_letter_max_attempts
  }

  retry_policy {
    minimum_backoff = local.retry_min_backoff
    maximum_backoff = local.retry_max_backoff
  }
}

# henchmen-ci-failure → Mastermind
resource "google_pubsub_topic" "ci_failure" {
  project = var.project_id
  name    = "henchmen-${var.environment}-ci-failure"
  labels  = var.labels
}

resource "google_pubsub_subscription" "ci_failure" {
  project = var.project_id
  name    = "henchmen-${var.environment}-ci-failure-sub"
  topic   = google_pubsub_topic.ci_failure.name

  message_retention_duration = local.retention_duration
  ack_deadline_seconds       = local.ack_deadline_seconds

  push_config {
    push_endpoint = "${var.push_endpoints.mastermind_url}/pubsub/ci-failure"
    oidc_token {
      service_account_email = var.push_sa_email
      audience              = var.push_endpoints.mastermind_url
    }
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = local.dead_letter_max_attempts
  }

  retry_policy {
    minimum_backoff = local.retry_min_backoff
    maximum_backoff = local.retry_max_backoff
  }
}

# henchmen-dead-letter → pull (alerting/monitoring)
resource "google_pubsub_subscription" "dead_letter" {
  project = var.project_id
  name    = "henchmen-${var.environment}-dead-letter-sub"
  topic   = google_pubsub_topic.dead_letter.name

  message_retention_duration   = local.retention_duration
  ack_deadline_seconds         = local.ack_deadline_seconds
  enable_exactly_once_delivery = true

  retry_policy {
    minimum_backoff = local.retry_min_backoff
    maximum_backoff = local.retry_max_backoff
  }
}
