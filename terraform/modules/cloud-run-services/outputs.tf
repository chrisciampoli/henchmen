output "service_urls" {
  description = "Map of component name to Cloud Run service URL"
  value = {
    mastermind = google_cloud_run_v2_service.mastermind.uri
    arsenal    = google_cloud_run_v2_service.arsenal.uri
    dispatch   = google_cloud_run_v2_service.dispatch.uri
    forge      = google_cloud_run_v2_service.forge.uri
  }
}

output "service_names" {
  description = "Map of component name to fully-qualified Cloud Run service resource name"
  value = {
    mastermind = google_cloud_run_v2_service.mastermind.name
    arsenal    = google_cloud_run_v2_service.arsenal.name
    dispatch   = google_cloud_run_v2_service.dispatch.name
    forge      = google_cloud_run_v2_service.forge.name
  }
}
