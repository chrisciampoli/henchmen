# scheduler

Cloud Scheduler jobs for periodic maintenance: stale task cleanup (every 6 hours), merge queue processing (every 5 minutes), stalled-task watchdog (every 5 minutes), and dead-letter queue check (every 15 minutes). Every job authenticates with an OIDC token for the dedicated Scheduler service account, whose `audience` is the fixed audience the target service registers via `custom_audiences` — not the service URL — so the token the service verifies always matches.

## Usage

```hcl
module "scheduler" {
  source              = "../../modules/scheduler"
  project_id          = var.project_id
  region              = var.region
  environment         = var.environment
  mastermind_url      = module.cloud_run_services.service_urls["mastermind"]
  forge_url           = module.cloud_run_services.service_urls["forge"]
  mastermind_audience = module.cloud_run_services.pubsub_audiences["mastermind"]
  forge_audience      = module.cloud_run_services.pubsub_audiences["forge"]
  scheduler_sa_email  = module.iam.service_account_emails["scheduler"]
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region for Cloud Scheduler jobs. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| mastermind_url | string | (required) | The base URL of the Mastermind Cloud Run service. |
| forge_url | string | (required) | The base URL of the Forge Cloud Run service. |
| mastermind_audience | string | (required) | OIDC audience registered on Mastermind. |
| forge_audience | string | (required) | OIDC audience registered on Forge. |
| scheduler_sa_email | string | (required) | Service account Cloud Scheduler mints OIDC tokens for. |

## Outputs

| Name | Description |
|---|---|
| cleanup_job_name | The name of the stale-task-cleanup job. |
| merge_queue_job_name | The name of the merge-queue-processor job. |
| watchdog_job_name | The name of the watchdog job. |
| dlq_check_job_name | The name of the DLQ check job. |

## Resources created

- `google_cloud_scheduler_job.stale_task_cleanup` — POSTs to `${mastermind_url}/api/v1/cleanup` every 6 hours.
- `google_cloud_scheduler_job.merge_queue_processor` — POSTs to `${forge_url}/api/v1/process-queue` every 5 minutes.
- `google_cloud_scheduler_job.watchdog` — POSTs to `${mastermind_url}/api/v1/watchdog` every 5 minutes.
- `google_cloud_scheduler_job.dlq_check` — POSTs to `${mastermind_url}/api/v1/check-dlq` every 15 minutes.

Cloud Scheduler jobs do not support labels, so this module takes none.
