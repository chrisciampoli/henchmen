output "cleanup_job_name" {
  description = "The name of the stale-task-cleanup Cloud Scheduler job"
  value       = google_cloud_scheduler_job.stale_task_cleanup.name
}

output "merge_queue_job_name" {
  description = "The name of the merge-queue-processor Cloud Scheduler job"
  value       = google_cloud_scheduler_job.merge_queue_processor.name
}

output "watchdog_job_name" {
  description = "The name of the watchdog Cloud Scheduler job"
  value       = google_cloud_scheduler_job.watchdog.name
}

output "dlq_check_job_name" {
  description = "The name of the DLQ check Cloud Scheduler job"
  value       = google_cloud_scheduler_job.dlq_check.name
}
