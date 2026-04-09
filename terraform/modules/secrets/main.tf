# ---------------------------------------------------------------------------
# Secret Manager secrets
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

resource "google_secret_manager_secret" "jira_api_token" {
  project   = var.project_id
  secret_id = "henchmen-${var.environment}-jira-api-token"

  replication {
    auto {}
  }

  labels = var.labels
}

# ---------------------------------------------------------------------------
# IAM access: henchmen-github-token -> sa-mastermind, sa-operative, sa-forge, sa-dossier
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret_iam_member" "github_token_mastermind" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.github_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_emails["mastermind"]}"
}

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

resource "google_secret_manager_secret_iam_member" "github_token_dossier" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.github_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_account_emails["dossier"]}"
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

resource "google_secret_manager_secret_iam_member" "slack_signing_secret_dispatch" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.slack_signing_secret.secret_id
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
