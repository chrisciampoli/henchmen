# cloud-run-services

Deploys the four long-running Henchmen Cloud Run v2 services — Mastermind, Arsenal, Dispatch, and Forge — onto the VPC connector from the `networking` module, with per-service IAM from the `iam` module. Secrets are mounted as environment variables from Secret Manager; container images default to the Artifact Registry paths created by `artifact-registry` but can be overridden. The module also grants `roles/run.invoker` to the Pub/Sub push service account on every service so push subscriptions from the `pubsub` module can deliver.

## Usage

```hcl
module "cloud_run_services" {
  source                 = "../../modules/cloud-run-services"
  project_id             = var.project_id
  region                 = var.region
  environment            = var.environment
  vpc_connector_id       = module.networking.connector_id
  service_account_emails = module.iam.service_account_emails
  pubsub_push_sa_email   = module.iam.service_account_emails["mastermind"]
  github_default_repo    = var.github_default_repo
  container_image_tag    = var.container_image_tag
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region to deploy Cloud Run services into. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| labels | map(string) | `{}` | Labels to apply to Cloud Run services. |
| vpc_connector_id | string | (required) | The ID of the VPC Serverless Access Connector (from networking module). |
| service_account_emails | map(string) | (required) | Map of component name to service account email (from iam module). |
| container_images | map(string) | `{}` | Map of component name to container image URL override. Defaults to Artifact Registry paths. |
| github_default_repo | string | `""` | Default GitHub repository for operatives (e.g. owner/repo). |
| pubsub_push_sa_email | string | (required) | Service account email used by Pub/Sub to authenticate push deliveries to Cloud Run. |
| container_image_tag | string | `latest` | Container image tag to deploy (e.g. a git short SHA or `latest`). |

## Outputs

| Name | Description |
|---|---|
| service_urls | Map of component name to Cloud Run service URL. |
| service_names | Map of component name to fully-qualified Cloud Run service resource name. |

## Resources created

- `google_cloud_run_v2_service.mastermind` — Orchestrator (2 vCPU, 4Gi, 60-minute timeout).
- `google_cloud_run_v2_service.arsenal` — MCP tool server (1 vCPU, 512Mi).
- `google_cloud_run_v2_service.dispatch` — Intake router (1 vCPU, 512Mi) with Slack secrets mounted.
- `google_cloud_run_v2_service.forge` — CI/merge queue service (1 vCPU, 512Mi) with GitHub token mounted.
- `google_cloud_run_v2_service_iam_member.pubsub_invoker` — Grants `roles/run.invoker` to the Pub/Sub push SA on each service.
