resource "google_artifact_registry_repository" "henchmen" {
  project       = var.project_id
  location      = var.region
  repository_id = "henchmen-${var.environment}"
  format        = "DOCKER"
  labels        = var.labels
}
