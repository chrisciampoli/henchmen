# cloud-run-lairs

Provisions the canonical Cloud Run Job template that Mastermind clones (via the Jobs API) every time it needs to spawn an ephemeral Operative Lair. The job itself is never executed directly — Mastermind creates per-task job executions with overridden environment variables and a fresh lair identity at dispatch time. After a new operative image is built and pushed, this template must be refreshed so clones pick up the new image.

## Usage

```hcl
module "cloud_run_lairs" {
  source             = "../../modules/cloud-run-lairs"
  project_id         = var.project_id
  region             = var.region
  environment        = var.environment
  vpc_connector_id   = module.networking.connector_id
  operative_sa_email = module.iam.operative_sa_email
  operative_image    = "${var.region}-docker.pkg.dev/${var.project_id}/henchmen-${var.environment}/operative:latest"
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region to deploy the Cloud Run Job template into. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| labels | map(string) | `{}` | Labels to apply to Cloud Run Job resources. |
| vpc_connector_id | string | (required) | The ID of the VPC Serverless Access Connector (from networking module). |
| operative_sa_email | string | (required) | Service account email for Operative Cloud Run Jobs (from iam module). |
| operative_image | string | (required) | Container image URL for the Operative runtime. |
| lair_cpu | string | `4` | CPU limit for each Lair container (vCPU). |
| lair_memory | string | `8Gi` | Memory limit for each Lair container. |
| lair_timeout | number | `1800` | Maximum execution duration for a Lair job, in seconds. |

## Outputs

| Name | Description |
|---|---|
| job_name | The name of the Lair template Cloud Run Job. |
| job_id | The fully-qualified resource ID of the Lair template Cloud Run Job. |

## Resources created

- `google_cloud_run_v2_job.lair_template` — Template Cloud Run Job (`henchmen-${environment}-lair-template`) with 4 vCPU / 8Gi / 1800s defaults and `max_retries = 0`.
