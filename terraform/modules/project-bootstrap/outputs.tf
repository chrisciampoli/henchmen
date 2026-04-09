output "enabled_apis" {
  description = "List of enabled GCP API service names"
  value       = [for svc in google_project_service.apis : svc.service]
}
