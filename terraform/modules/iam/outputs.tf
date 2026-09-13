output "mastermind_sa_email" {
  description = "Email of the Mastermind service account"
  value       = google_service_account.mastermind.email
}

output "mastermind_sa_id" {
  description = "ID of the Mastermind service account"
  value       = google_service_account.mastermind.id
}

output "dispatch_sa_email" {
  description = "Email of the Dispatch service account"
  value       = google_service_account.dispatch.email
}

output "dispatch_sa_id" {
  description = "ID of the Dispatch service account"
  value       = google_service_account.dispatch.id
}

output "operative_sa_email" {
  description = "Email of the Operative service account"
  value       = google_service_account.operative.email
}

output "operative_sa_id" {
  description = "ID of the Operative service account"
  value       = google_service_account.operative.id
}

output "forge_sa_email" {
  description = "Email of the Forge service account"
  value       = google_service_account.forge.email
}

output "forge_sa_id" {
  description = "ID of the Forge service account"
  value       = google_service_account.forge.id
}

output "pubsub_push_sa_email" {
  description = "Email of the Pub/Sub push (OIDC caller) service account"
  value       = google_service_account.pubsub_push.email
}

output "scheduler_sa_email" {
  description = "Email of the Cloud Scheduler (OIDC caller) service account"
  value       = google_service_account.scheduler.email
}

output "service_account_emails" {
  description = "Map of service name to service account email"
  value = {
    mastermind  = google_service_account.mastermind.email
    dispatch    = google_service_account.dispatch.email
    operative   = google_service_account.operative.email
    forge       = google_service_account.forge.email
    pubsub_push = google_service_account.pubsub_push.email
    scheduler   = google_service_account.scheduler.email
  }
}
