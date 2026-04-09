# project-bootstrap

Enables the GCP APIs that every other Henchmen module depends on. This is the first module that should run against a fresh project — if its APIs are not enabled, downstream modules will fail with `SERVICE_DISABLED` errors. It does not create projects, link billing, or provision the Terraform state bucket; those steps are handled by `scripts/bootstrap-gcp.sh` before `terraform apply`.

## Usage

```hcl
module "project_bootstrap" {
  source     = "../../modules/project-bootstrap"
  project_id = var.project_id
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID in which to enable APIs. |

## Outputs

| Name | Description |
|---|---|
| enabled_apis | List of enabled GCP API service names. |

## Resources created

- `google_project_service.apis` — Enables 13 APIs: Cloud Run, Cloud Build, Pub/Sub, Firestore, Secret Manager, Artifact Registry, Logging, Monitoring, Cloud Scheduler, Vertex AI, Compute, VPC Access, and IAM.
