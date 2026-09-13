# ---------------------------------------------------------------------------
# Secret Manager secrets
#
# Cloud Run refuses to start a revision that mounts a secret with no version,
# so every secret mounted by the cloud-run-services / cloud-run-lairs modules
# is seeded with a placeholder version (see var.seed_secret_placeholders).
# Operators then add the real value as a NEW version, which becomes `latest`:
#   gcloud secrets versions add henchmen-<env>-github-token --data-file=-
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret" "github_token" {
  project   = var.project_id
  secret_id = "henchmen-${var.environment}-github-token"

  replication {
    auto {}
  }

  labels = var.labels
}

resource "google_secret_manager_secret" "slack_bot_token" {
  project   = var.project_id
  secret_id = "henchmen-${var.environment}-slack-bot-token"

  replication {
    auto {}
  }

  labels = var.labels
}

resource "google_secret_manager_secret" "slack_signing_secret" {
  project   = var.project_id
  secret_id = "henchmen-${var.environment}-slack-signing-secret"

  replication {
    auto {}
  }

  labels = var.labels
}

resource "google_secret_manager_secret" "slack_app_token" {
  project   = var.project_id
  secret_id = "henchmen-${var.environment}-slack-app-token"

  replication {
    auto {}
  }

  labels = var.labels
}

resource "google_secret_manager_secret" "jira_api_token" {
  project   = var.project_id
  secret_id = "henchmen-${var.environment}-jira-api-token"

  replication {
    auto {}
  }

  labels = var.labels
}

# Bearer token the /metrics endpoints require (HENCHMEN_METRICS_AUTH_TOKEN).
# Settings treats an empty token as "open with a warning" in dev and as 401 in
# staging/prod, so this must hold a real, high-entropy value before staging.
resource "google_secret_manager_secret" "metrics_auth_token" {
  project   = var.project_id
  secret_id = "henchmen-${var.environment}-metrics-auth-token"

  replication {
    auto {}
  }

  labels = var.labels
}

# ---------------------------------------------------------------------------
# Placeholder versions
#
# These exist only so the very first apply on a fresh project produces
# startable Cloud Run revisions. They are NOT usable credentials: every one of
# them fails loudly at the first API call. `ignore_changes` keeps terraform
# from rewriting them, and adding a real version through gcloud supersedes
# them because Cloud Run mounts `latest`.
#
# On a project that already holds real secret versions, set
# seed_secret_placeholders = false BEFORE applying: creating a placeholder
# version now would make it `latest` and shadow the real value.
# ---------------------------------------------------------------------------

locals {
  seeded_secrets = var.seed_secret_placeholders ? {
    github_token         = google_secret_manager_secret.github_token.id
    slack_bot_token      = google_secret_manager_secret.slack_bot_token.id
    slack_signing_secret = google_secret_manager_secret.slack_signing_secret.id
    slack_app_token      = google_secret_manager_secret.slack_app_token.id
    jira_api_token       = google_secret_manager_secret.jira_api_token.id
    metrics_auth_token   = google_secret_manager_secret.metrics_auth_token.id
  } : {}
}

resource "google_secret_manager_secret_version" "placeholder" {
  for_each = local.seeded_secrets

  secret      = each.value
  secret_data = "placeholder-replace-with-a-real-value"

  lifecycle {
    ignore_changes = [secret_data]
  }
}

# ---------------------------------------------------------------------------
# IAM access: henchmen-github-token -> sa-mastermind, sa-operative, sa-forge
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret_iam_member" "github_token_mastermind" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.github_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_emails["mastermind"]}"
}

# The operative reads this at job start (GITHUB_TOKEN mount on the lair) to
# clone and push.
resource "google_secret_manager_secret_iam_member" "github_token_operative" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.github_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_emails["operative"]}"
}

resource "google_secret_manager_secret_iam_member" "github_token_forge" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.github_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_emails["forge"]}"
}

# ---------------------------------------------------------------------------
# IAM access: slack tokens -> sa-dispatch
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret_iam_member" "slack_bot_token_dispatch" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.slack_bot_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_emails["dispatch"]}"
}

# Mastermind also mounts SLACK_BOT_TOKEN (see cloud-run-services module) so it
# needs accessor role on the same secret.
resource "google_secret_manager_secret_iam_member" "slack_bot_token_mastermind" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.slack_bot_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_emails["mastermind"]}"
}

resource "google_secret_manager_secret_iam_member" "slack_signing_secret_dispatch" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.slack_signing_secret.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_emails["dispatch"]}"
}

resource "google_secret_manager_secret_iam_member" "slack_app_token_dispatch" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.slack_app_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_emails["dispatch"]}"
}

# ---------------------------------------------------------------------------
# IAM access: henchmen-jira-api-token -> sa-dispatch
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret_iam_member" "jira_api_token_dispatch" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.jira_api_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_emails["dispatch"]}"
}

# ---------------------------------------------------------------------------
# IAM access: henchmen-metrics-auth-token -> every HTTP service
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret_iam_member" "metrics_auth_token" {
  for_each = toset(["mastermind", "dispatch", "forge"])

  project   = var.project_id
  secret_id = google_secret_manager_secret.metrics_auth_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_emails[each.value]}"
}
