output "service_urls" {
  description = "Map of component name to Cloud Run service URL"
  value = {
    mastermind = google_cloud_run_v2_service.mastermind.uri
    dispatch   = google_cloud_run_v2_service.dispatch.uri
    forge      = google_cloud_run_v2_service.forge.uri
  }
}

output "service_names" {
  description = "Map of component name to Cloud Run service name"
  value       = local.service_names
}

output "pubsub_audiences" {
  description = "Map of component name to the OIDC audience the service accepts and verifies (registered via custom_audiences)"
  value       = local.pubsub_audiences
}
