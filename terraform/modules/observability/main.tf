# ---------------------------------------------------------------------------
# Log sink
#
# The destination bucket is created here: a sink pointing at a log bucket that
# does not exist accepts the apply and then fails every export, and the sink's
# writer identity needs roles/logging.bucketWriter or the export is denied.
# ---------------------------------------------------------------------------

resource "google_logging_project_bucket_config" "henchmen" {
  project        = var.project_id
  location       = var.region
  bucket_id      = "henchmen-${var.environment}-logs"
  retention_days = var.log_retention_days
  description    = "Cloud Run revision and job logs for Henchmen ${var.environment}"
}

resource "google_logging_project_sink" "henchmen_logs" {
  project                = var.project_id
  name                   = "henchmen-${var.environment}-log-sink"
  destination            = "logging.googleapis.com/${google_logging_project_bucket_config.henchmen.id}"
  filter                 = "resource.type=\"cloud_run_revision\" OR resource.type=\"cloud_run_job\""
  unique_writer_identity = true
}

resource "google_project_iam_member" "log_sink_writer" {
  project = var.project_id
  role    = "roles/logging.bucketWriter"
  member  = google_logging_project_sink.henchmen_logs.writer_identity
}

# ---------------------------------------------------------------------------
# Alert policies
#
# Every policy below is built on a metric Cloud Run or Pub/Sub emits on its
# own. Henchmen writes no custom Cloud Monitoring time series (its metrics go
# to Firestore), so custom.googleapis.com/* policies would never fire and are
# deliberately absent.
# ---------------------------------------------------------------------------

resource "google_monitoring_alert_policy" "lair_timeout" {
  project      = var.project_id
  display_name = "Lair Timeout Alert"
  combiner     = "OR"

  conditions {
    display_name = "Lair execution timed out"

    condition_threshold {
      filter          = "resource.type = \"cloud_run_job\" AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.labels.result = \"failed\""
      duration        = "0s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = var.notification_channels

  alert_strategy {
    auto_close = "1800s"
  }
}

resource "google_monitoring_alert_policy" "dead_letter_depth" {
  project      = var.project_id
  display_name = "Dead Letter Queue Alert"
  combiner     = "OR"

  conditions {
    display_name = "Dead letter queue has messages"

    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND metric.type = \"pubsub.googleapis.com/subscription/num_undelivered_messages\" AND resource.labels.subscription_id = \"henchmen-${var.environment}-dead-letter-sub\""
      duration        = "60s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = var.notification_channels

  alert_strategy {
    auto_close = "1800s"
  }
}

# 5xx from any Henchmen service. Catches the failure mode the DLQ alert cannot
# see: pushes that are rejected (401/500) faster than they dead-letter.
resource "google_monitoring_alert_policy" "service_errors" {
  project      = var.project_id
  display_name = "Henchmen Service Error Rate Alert"
  combiner     = "OR"

  conditions {
    display_name = "Cloud Run 5xx responses"

    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\" AND resource.labels.service_name = starts_with(\"henchmen-${var.environment}-\")"
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0

      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = var.notification_channels

  alert_strategy {
    auto_close = "1800s"
  }
}

# ---------------------------------------------------------------------------
# Monitoring dashboard
# ---------------------------------------------------------------------------

resource "google_monitoring_dashboard" "henchmen" {
  project = var.project_id

  dashboard_json = jsonencode({
    displayName = "Henchmen ${var.environment}"
    mosaicLayout = {
      columns = 12
      tiles = [
        {
          width  = 6
          height = 4
          widget = {
            title = "Requests by service"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloud_run_revision\" AND metric.type=\"run.googleapis.com/request_count\" AND resource.labels.service_name=monitoring.regex.full_match(\"henchmen-${var.environment}-.*\")"
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_RATE"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["resource.labels.service_name"]
                    }
                  }
                }
              }]
            }
          }
        },
        {
          xPos   = 6
          width  = 6
          height = 4
          widget = {
            title = "Request latency p95"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloud_run_revision\" AND metric.type=\"run.googleapis.com/request_latencies\" AND resource.labels.service_name=monitoring.regex.full_match(\"henchmen-${var.environment}-.*\")"
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_95"
                      crossSeriesReducer = "REDUCE_MEAN"
                      groupByFields      = ["resource.labels.service_name"]
                    }
                  }
                }
              }]
            }
          }
        },
        {
          yPos   = 4
          width  = 6
          height = 4
          widget = {
            title = "Lair executions by result"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloud_run_job\" AND metric.type=\"run.googleapis.com/job/completed_execution_count\""
                    aggregation = {
                      alignmentPeriod    = "3600s"
                      perSeriesAligner   = "ALIGN_DELTA"
                      crossSeriesReducer = "REDUCE_SUM"
                      groupByFields      = ["metric.labels.result"]
                    }
                  }
                }
              }]
            }
          }
        },
        {
          xPos   = 6
          yPos   = 4
          width  = 6
          height = 4
          widget = {
            title = "Dead Letter Queue Depth"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"pubsub_subscription\" AND metric.type=\"pubsub.googleapis.com/subscription/num_undelivered_messages\" AND resource.labels.subscription_id=monitoring.regex.full_match(\"henchmen-${var.environment}-dead-letter-sub\")"
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_MEAN"
                      crossSeriesReducer = "REDUCE_SUM"
                    }
                  }
                }
              }]
            }
          }
        },
        {
          yPos   = 8
          width  = 12
          height = 4
          widget = {
            title = "Active Lairs (Cloud Run Job Executions)"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "resource.type=\"cloud_run_job\" AND metric.type=\"run.googleapis.com/job/running_executions\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_MEAN"
                      crossSeriesReducer = "REDUCE_SUM"
                    }
                  }
                }
              }]
            }
          }
        },
      ]
    }
  })
}
