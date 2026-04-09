output "repository_id" {
  description = "The Artifact Registry repository ID"
  value       = google_artifact_registry_repository.henchmen.repository_id
}

output "repository_url" {
  description = "The Docker pull/push URL for the Artifact Registry repository"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.henchmen.repository_id}"
}
