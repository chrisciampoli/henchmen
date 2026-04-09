# scheduler

Provisions the Cloud Scheduler cron jobs Henchmen relies on for periodic maintenance: stale task cleanup (every 6 hours), merge queue processing (every 5 minutes), stalled-task watchdog (every 5 minutes), and dead-letter queue check (every 15 minutes). Every job uses OIDC to authenticate against the target Cloud Run service, with the `audience` set to the service URL to avoid silent 403s.

## Usage

```hcl
module "scheduler" {
  source             = "../../modules/scheduler"
  project_id         = var.project_id
  region             = var.region
  environment        = var.environment
  mastermind_url     = module.cloud_run_services.service_urls["mastermind"]
  forge_url          = module.cloud_run_services.service_urls["forge"]
  scheduler_sa_email = module.iam.mastermind_sa_email
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region for Cloud Scheduler jobs. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| labels | map(string) | `{}` | Labels to apply to Cloud Scheduler jobs. |
| mastermind_url | string | (required) | The base URL of the Mastermind Cloud Run service. |
| forge_url | string | (required) | The base URL of the Forge Cloud Run service. |
| scheduler_sa_email | string | (required) | Service account email used by Cloud Scheduler to authenticate OIDC tokens. |

## Outputs

| Name | Description |
|---|---|
| cleanup_job_name | The name of the stale-task-cleanup Cloud Scheduler job. |
| merge_queue_job_name | The name of the merge-queue-processor Cloud Scheduler job. |
| watchdog_job_name | The name of the watchdog Cloud Scheduler job. |
| dlq_check_job_name | The name of the DLQ check Cloud Scheduler job. |

## Resources created

- `google_cloud_scheduler_job.stale_task_cleanup` — POSTs to `${mastermind_url}/api/v1/cleanup` every 6 hours.
- `google_cloud_scheduler_job.merge_queue_processor` — POSTs to `${forge_url}/api/v1/process-queue` every 5 minutes.
- `google_cloud_scheduler_job.watchdog` — POSTs to `${mastermind_url}/api/v1/watchdog` every 5 minutes.
- `google_cloud_scheduler_job.dlq_check` — POSTs to `${mastermind_url}/api/v1/check-dlq` every 15 minutes.
