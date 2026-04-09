# observability

Sets up the Henchmen monitoring surface: a Cloud Logging sink for Cloud Run revisions and jobs, three custom metric descriptors (`lair_duration`, `ci_pass_rate`, `task_throughput`) that Mastermind/Forge write at runtime, three alert policies (lair timeouts, dead-letter queue depth, CI failure rate), and a Cloud Monitoring dashboard that charts all of them. Notification channels are injected as an input so operators can wire alerts to their preferred targets.

## Usage

```hcl
module "observability" {
  source                = "../../modules/observability"
  project_id            = var.project_id
  region                = var.region
  environment           = var.environment
  notification_channels = var.notification_channels
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region for regional observability resources. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| labels | map(string) | `{}` | Labels to apply to observability resources. |
| notification_channels | list(string) | `[]` | List of Cloud Monitoring notification channel resource names to attach to alert policies. |

## Outputs

| Name | Description |
|---|---|
| log_sink_name | The name of the Henchmen log sink. |
| dashboard_id | The resource name of the Henchmen monitoring dashboard. |
| alert_policy_ids | Map of alert policy name to resource name. |

## Resources created

- `google_logging_project_sink.henchmen_logs` — Routes Cloud Run revision and job logs to a dedicated log bucket.
- `google_monitoring_metric_descriptor` — Three custom metrics: lair_duration, ci_pass_rate, task_throughput.
- `google_monitoring_alert_policy` — Three policies: lair timeout, dead-letter queue depth > 0, CI failure rate > 50% over 1 hour.
- `google_monitoring_dashboard.henchmen` — Dashboard with tiles for task throughput, lair duration, CI pass rate, DLQ depth, and active lairs.
