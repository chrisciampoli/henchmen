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

output "arsenal_sa_email" {
  description = "Email of the Arsenal service account"
  value       = google_service_account.arsenal.email
}

output "arsenal_sa_id" {
  description = "ID of the Arsenal service account"
  value       = google_service_account.arsenal.id
}

output "forge_sa_email" {
  description = "Email of the Forge service account"
  value       = google_service_account.forge.email
}

output "forge_sa_id" {
  description = "ID of the Forge service account"
  value       = google_service_account.forge.id
}

output "dossier_sa_email" {
  description = "Email of the Dossier service account"
  value       = google_service_account.dossier.email
}

output "dossier_sa_id" {
  description = "ID of the Dossier service account"
  value       = google_service_account.dossier.id
}

output "service_account_emails" {
  description = "Map of service name to service account email"
  value = {
    mastermind = google_service_account.mastermind.email
    dispatch   = google_service_account.dispatch.email
    operative  = google_service_account.operative.email
    arsenal    = google_service_account.arsenal.email
    forge      = google_service_account.forge.email
    dossier    = google_service_account.dossier.email
  }
}
