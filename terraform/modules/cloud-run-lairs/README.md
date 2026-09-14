# cloud-run-lairs

Reference definition of an Operative Lair as a Cloud Run Job (`henchmen-${environment}-lair-template`): image, service account, sizing, environment and the `GITHUB_TOKEN` secret mount a lair runs with.

Mastermind does **not** clone or execute this job at runtime. For every scheme node it creates a fresh `lair-<task>-<node>` job through the Cloud Run Jobs API, using the `HENCHMEN_LAIR_*` values injected into the Mastermind service by the `cloud-run-services` module — which is fed from the same `lair_cpu` / `lair_memory` / `lair_timeout` inputs as this module, so the two stay in sync. The template exists so the rendered job spec is reviewable in Terraform and so `gcloud run jobs execute` can smoke-test a freshly pushed operative image by hand.

## Usage

```hcl
module "cloud_run_lairs" {
  source               = "../../modules/cloud-run-lairs"
  project_id           = var.project_id
  region               = var.region
  environment          = var.environment
  labels               = local.labels
  vpc_connector_id     = module.networking.connector_id
  operative_sa_email   = module.iam.service_account_emails["operative"]
  operative_image      = "${module.artifact_registry.repository_url}/operative:${var.container_image_tag}"
  firestore_database   = module.data_stores.database_name
  gcs_bucket_dossier   = module.data_stores.dossier_bucket_name
  gcs_bucket_snapshots = module.data_stores.snapshots_bucket_name
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region to deploy the job template into. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| labels | map(string) | `{}` | Labels to apply to the job. |
| vpc_connector_id | string | (required) | The ID of the VPC Serverless Access Connector. |
| operative_sa_email | string | (required) | Service account the lair runs as. |
| operative_image | string | (required) | Container image URL for the Operative runtime. |
| firestore_database | string | (required) | Firestore database name. |
| gcs_bucket_dossier | string | `""` | Dossier artifact bucket name. |
| gcs_bucket_snapshots | string | `""` | Operative snapshot bucket name. |
| lair_cpu | string | `"4"` | CPU limit for the lair container (vCPU). |
| lair_memory | string | `"8Gi"` | Memory limit for the lair container. |
| lair_timeout | number | `1800` | Maximum execution duration, in seconds. |

## Outputs

| Name | Description |
|---|---|
| job_name | The name of the Lair template Cloud Run Job. |
| job_id | The fully-qualified resource ID of the Lair template Cloud Run Job. |

## Resources created

- `google_cloud_run_v2_job.lair_template` — Template job with `max_retries = 0`, private-range VPC egress, and the sizing above.
