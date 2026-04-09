output "log_sink_name" {
  description = "The name of the Henchmen log sink"
  value       = google_logging_project_sink.henchmen_logs.name
}

output "dashboard_id" {
  description = "The resource name of the Henchmen monitoring dashboard"
  value       = google_monitoring_dashboard.henchmen.id
}

output "alert_policy_ids" {
  description = "Map of alert policy name to resource name"
  value = {
    lair_timeout     = google_monitoring_alert_policy.lair_timeout.name
    dead_letter      = google_monitoring_alert_policy.dead_letter_depth.name
    ci_failure_rate  = google_monitoring_alert_policy.ci_failure_rate.name
  }
}
