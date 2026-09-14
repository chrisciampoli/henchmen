# project-bootstrap

Enables the GCP APIs that every other Henchmen module depends on. This is the first module that should run against a fresh project — if its APIs are not enabled, downstream modules fail with `SERVICE_DISABLED` errors. It does not create projects, link billing, or provision the Terraform state bucket; those steps are handled by `scripts/bootstrap-gcp.sh` before `terraform apply`.

APIs are never disabled on destroy (`disable_on_destroy = false`), so tearing down an environment cannot break other workloads in the same project.

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

- `google_project_service.apis` — Enables 17 APIs: Cloud Resource Manager (needed before any project IAM binding can be applied), Service Usage, Cloud Run, Cloud Build, Pub/Sub, Firestore, Secret Manager, Cloud Storage, Artifact Registry, Logging, Monitoring, Cloud Trace (every service account holds `roles/cloudtrace.agent`), Cloud Scheduler, Vertex AI, Compute, Serverless VPC Access, and IAM.
