# ---------------------------------------------------------------------------
# Log sink
# ---------------------------------------------------------------------------

resource "google_logging_project_sink" "henchmen_logs" {
  project                = var.project_id
  name                   = "henchmen-${var.environment}-log-sink"
  destination            = "logging.googleapis.com/projects/${var.project_id}/locations/${var.region}/buckets/henchmen-${var.environment}-logs"
  filter                 = "resource.type=\"cloud_run_revision\" OR resource.type=\"cloud_run_job\""
  unique_writer_identity = true
}

# ---------------------------------------------------------------------------
# Custom metric descriptors
# ---------------------------------------------------------------------------

resource "google_monitoring_metric_descriptor" "lair_duration" {
  project      = var.project_id
  description  = "Duration of Lair (operative) executions"
  display_name = "Lair Duration"
  type         = "custom.googleapis.com/henchmen/lair_duration"
  metric_kind  = "GAUGE"
  value_type   = "DOUBLE"
  unit         = "s"

  labels {
    key = "scheme_id"
  }
  labels {
    key = "node_id"
  }
}

resource "google_monitoring_metric_descriptor" "ci_pass_rate" {
  project      = var.project_id
  description  = "CI pass rate percentage"
  display_name = "CI Pass Rate"
  type         = "custom.googleapis.com/henchmen/ci_pass_rate"
  metric_kind  = "GAUGE"
  value_type   = "DOUBLE"
  unit         = "%"
}

resource "google_monitoring_metric_descriptor" "task_throughput" {
  project      = var.project_id
  description  = "Tasks completed per hour"
  display_name = "Task Throughput"
  type         = "custom.googleapis.com/henchmen/task_throughput"
  metric_kind  = "GAUGE"
  value_type   = "INT64"
  unit         = "1/h"
}

# ---------------------------------------------------------------------------
# Alert policies
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

resource "google_monitoring_alert_policy" "ci_failure_rate" {
  project      = var.project_id
  display_name = "High CI Failure Rate Alert"
  combiner     = "OR"

  conditions {
    display_name = "CI failure rate >50% over 1 hour"

    condition_threshold {
      # Alert when the custom ci_pass_rate metric drops below 50%
      filter          = "resource.type = \"global\" AND metric.type = \"custom.googleapis.com/henchmen/ci_pass_rate\""
      duration        = "3600s"
      comparison      = "COMPARISON_LT"
      threshold_value = 50

      aggregations {
        alignment_period   = "3600s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = var.notification_channels

  alert_strategy {
    auto_close = "7200s"
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
            title = "Task Throughput"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"custom.googleapis.com/henchmen/task_throughput\""
                    aggregation = {
                      alignmentPeriod    = "3600s"
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
          xPos   = 6
          width  = 6
          height = 4
          widget = {
            title = "Lair Duration (p50/p95)"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"custom.googleapis.com/henchmen/lair_duration\""
                    aggregation = {
                      alignmentPeriod    = "60s"
                      perSeriesAligner   = "ALIGN_PERCENTILE_50"
                      crossSeriesReducer = "REDUCE_MEAN"
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
            title = "CI Pass Rate (%)"
            xyChart = {
              dataSets = [{
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"custom.googleapis.com/henchmen/ci_pass_rate\""
                    aggregation = {
                      alignmentPeriod    = "3600s"
                      perSeriesAligner   = "ALIGN_MEAN"
                      crossSeriesReducer = "REDUCE_MEAN"
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
