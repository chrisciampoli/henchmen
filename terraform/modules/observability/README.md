# observability

The Henchmen monitoring surface: a dedicated log bucket and the sink that routes Cloud Run revision and job logs into it, three alert policies, and a Cloud Monitoring dashboard. Every alert and tile is built on metrics Cloud Run and Pub/Sub emit on their own. Henchmen writes no custom Cloud Monitoring time series (its task and cost metrics go to Firestore), so there are no `custom.googleapis.com/*` descriptors, alerts or tiles — they would never receive data. Notification channels are an input so operators can wire alerts to their preferred targets.

## Usage

```hcl
module "observability" {
  source                = "../../modules/observability"
  project_id            = var.project_id
  region                = var.region
  environment           = var.environment
  log_retention_days    = var.log_retention_days
  notification_channels = var.notification_channels
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region for the log bucket. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| notification_channels | list(string) | `[]` | Cloud Monitoring notification channel resource names to attach to alert policies. |
| log_retention_days | number | `30` | Retention, in days, for the Henchmen log bucket. Must be > 0. |

## Outputs

| Name | Description |
|---|---|
| log_sink_name | The name of the Henchmen log sink. |
| log_bucket_id | The resource ID of the log bucket the sink writes to. |
| dashboard_id | The resource name of the Henchmen monitoring dashboard. |
| alert_policy_ids | Map of alert policy name (`lair_timeout`, `dead_letter`, `service_errors`) to resource name. |

## Resources created

- `google_logging_project_bucket_config.henchmen` — Log bucket `henchmen-${environment}-logs` in `region`.
- `google_logging_project_sink.henchmen_logs` — Routes `cloud_run_revision` and `cloud_run_job` logs to that bucket with a unique writer identity.
- `google_project_iam_member.log_sink_writer` — Grants the sink's writer identity `roles/logging.bucketWriter`; without it every export is denied.
- `google_monitoring_alert_policy.lair_timeout` — Any failed Cloud Run Job execution.
- `google_monitoring_alert_policy.dead_letter_depth` — Undelivered messages on `henchmen-${environment}-dead-letter-sub` for 60s.
- `google_monitoring_alert_policy.service_errors` — Sustained 5xx responses from any `henchmen-${environment}-*` service.
- `google_monitoring_dashboard.henchmen` — Tiles for requests by service, p95 latency, lair executions by result, DLQ depth and running lair executions.
