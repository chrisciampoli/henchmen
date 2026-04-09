output "github_token_secret_id" {
  description = "Secret Manager secret ID for the GitHub token"
  value       = google_secret_manager_secret.github_token.secret_id
}

output "slack_bot_token_secret_id" {
  description = "Secret Manager secret ID for the Slack bot token"
  value       = google_secret_manager_secret.slack_bot_token.secret_id
}

output "slack_signing_secret_id" {
  description = "Secret Manager secret ID for the Slack signing secret"
  value       = google_secret_manager_secret.slack_signing_secret.secret_id
}

output "slack_app_token_secret_id" {
  description = "Secret Manager secret ID for the Slack app token (Socket Mode)"
  value       = google_secret_manager_secret.slack_app_token.secret_id
}

output "jira_api_token_secret_id" {
  description = "Secret Manager secret ID for the Jira API token"
  value       = google_secret_manager_secret.jira_api_token.secret_id
}

output "secret_ids" {
  description = "Map of logical secret name to Secret Manager secret ID"
  value = {
    github_token         = google_secret_manager_secret.github_token.secret_id
    slack_bot_token      = google_secret_manager_secret.slack_bot_token.secret_id
    slack_signing_secret = google_secret_manager_secret.slack_signing_secret.secret_id
    slack_app_token      = google_secret_manager_secret.slack_app_token.secret_id
    jira_api_token       = google_secret_manager_secret.jira_api_token.secret_id
  }
}
