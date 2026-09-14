# cloud-run-services

Deploys the three long-running Henchmen Cloud Run v2 services — Mastermind, Dispatch and Forge — on the VPC connector from the `networking` module. Arsenal is not a service: it is the tool registry that runs in-process inside the Operative, and no arsenal image is built.

- **Images.** Each service uses `container_images[<name>]` if set, otherwise `<region>-docker.pkg.dev/<project>/henchmen-<env>/<name>:<container_image_tag>`. While `container_image_tag` is empty (the default) every service deploys `placeholder_image`, so the first apply on a fresh project converges before any Henchmen image has been pushed.
- **Environment.** Terraform owns the full container environment: `HENCHMEN_GCP_PROJECT_ID`, `HENCHMEN_ENVIRONMENT`, `HENCHMEN_GCP_REGION`, `HENCHMEN_GITHUB_DEFAULT_REPO`, `HENCHMEN_FIRESTORE_DATABASE`, `HENCHMEN_GCS_BUCKET_DOSSIER`, `HENCHMEN_GCS_BUCKET_SNAPSHOTS`, `HENCHMEN_PUBSUB_OIDC_ALLOWED_EMAILS` and a per-service `HENCHMEN_PUBSUB_OIDC_AUDIENCE`; Mastermind also gets the `HENCHMEN_LAIR_*` sizing, image tag and service account; Dispatch gets `JIRA_SERVER` / `JIRA_EMAIL`. Empty values are omitted so application defaults apply. Anything set by hand with `gcloud run services update` is removed by the next apply — add it here instead.
- **Secrets.** Mounted from Secret Manager at `latest` under the bare names Settings accepts as aliases: Mastermind `GITHUB_TOKEN`, `SLACK_BOT_TOKEN`; Dispatch `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_APP_TOKEN`, `JIRA_API_TOKEN`, `DISPATCH_API_TOKEN` (the `/api/v1/tasks` bearer token); Forge `GITHUB_TOKEN`; all three `HENCHMEN_METRICS_AUTH_TOKEN`.
- **Pub/Sub audiences.** Each service registers the fixed audience `henchmen-<env>-<service>` via `custom_audiences`; the same value is exported for the `pubsub` and `scheduler` modules, so the signed and verified audiences agree by construction.
- **Ingress and invocation.** Mastermind and Forge use internal-only ingress (Pub/Sub push and Cloud Scheduler count as internal) unless `internal_only_ingress = false`. Dispatch keeps open ingress, but IAM still denies unauthenticated callers unless `dispatch_public_ingress = true` — required for GitHub and Jira webhooks, which cannot present a Google OIDC token. Only enable it once `/api/v1/tasks` is protected by an application-level token, since it lives on the same service.

## Usage

```hcl
module "cloud_run_services" {
  source                 = "../../modules/cloud-run-services"
  project_id             = var.project_id
  region                 = var.region
  environment            = var.environment
  labels                 = local.labels
  vpc_connector_id       = module.networking.connector_id
  service_account_emails = module.iam.service_account_emails
  pubsub_push_sa_email   = module.iam.service_account_emails["pubsub_push"]
  scheduler_sa_email     = module.iam.service_account_emails["scheduler"]
  firestore_database     = module.data_stores.database_name
  gcs_bucket_dossier     = module.data_stores.dossier_bucket_name
  gcs_bucket_snapshots   = module.data_stores.snapshots_bucket_name
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
| vpc_connector_id | string | (required) | The ID of the VPC Serverless Access Connector. |
| service_account_emails | map(string) | (required) | Service account emails from the iam module. Must contain `mastermind`, `dispatch`, `forge`, `operative`. |
| container_images | map(string) | `{}` | Per-service image overrides (keys: `mastermind`, `dispatch`, `forge`). |
| placeholder_image | string | `us-docker.pkg.dev/cloudrun/container/hello` | Image deployed while `container_image_tag` is empty. |
| github_default_repo | string | `""` | Default GitHub repository for operatives (`owner/repo`). |
| pubsub_push_sa_email | string | (required) | Pub/Sub push identity; granted `run.invoker` on every service and set as `HENCHMEN_PUBSUB_OIDC_ALLOWED_EMAILS`. |
| scheduler_sa_email | string | (required) | Cloud Scheduler identity; granted `run.invoker` on Mastermind and Forge. |
| container_image_tag | string | `""` | Image tag to deploy. Empty deploys `placeholder_image`. |
| firestore_database | string | (required) | Firestore database name. |
| gcs_bucket_dossier | string | `""` | Dossier artifact bucket name. |
| gcs_bucket_snapshots | string | `""` | Operative snapshot bucket name. |
| jira_base_url | string | `""` | Jira base URL, injected into Dispatch as `JIRA_SERVER`. |
| jira_email | string | `""` | Jira account email, injected into Dispatch as `JIRA_EMAIL`. |
| dispatch_public_ingress | bool | `false` | Grant `run.invoker` to `allUsers` on Dispatch. |
| internal_only_ingress | bool | `true` | Restrict Mastermind and Forge to internal ingress. |
| lair_cpu | string | `"4"` | vCPU Mastermind requests for each lair job (`HENCHMEN_LAIR_DEFAULT_CPU`). |
| lair_memory | string | `"8Gi"` | Memory Mastermind requests for each lair job (`HENCHMEN_LAIR_DEFAULT_MEMORY`). |
| lair_timeout | number | `1800` | Lair job timeout in seconds (`HENCHMEN_LAIR_DEFAULT_TIMEOUT`). |

## Outputs

| Name | Description |
|---|---|
| service_urls | Map of component name to Cloud Run service URL. |
| service_names | Map of component name to Cloud Run service name. |
| pubsub_audiences | Map of component name to the OIDC audience the service registers and verifies. |

## Resources created

- `google_cloud_run_v2_service.mastermind` — Orchestrator (2 vCPU, 4Gi, 60-minute request timeout).
- `google_cloud_run_v2_service.dispatch` — Intake router (1 vCPU, 512Mi).
- `google_cloud_run_v2_service.forge` — CI / merge queue service (1 vCPU, 512Mi).
- `google_cloud_run_v2_service_iam_member.pubsub_invoker` — `run.invoker` for the Pub/Sub push SA on all three services.
- `google_cloud_run_v2_service_iam_member.scheduler_invoker` — `run.invoker` for the Scheduler SA on Mastermind and Forge.
- `google_cloud_run_v2_service_iam_member.dispatch_public_invoker` — `run.invoker` for `allUsers` on Dispatch, only when `dispatch_public_ingress = true`.

Scaling is 0–3 instances outside `prod` and 1–10 in `prod`.
