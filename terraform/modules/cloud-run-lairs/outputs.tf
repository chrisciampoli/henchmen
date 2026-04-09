output "job_name" {
  description = "The name of the Lair template Cloud Run Job"
  value       = google_cloud_run_v2_job.lair_template.name
}

output "job_id" {
  description = "The fully-qualified resource ID of the Lair template Cloud Run Job"
  value       = google_cloud_run_v2_job.lair_template.id
}
