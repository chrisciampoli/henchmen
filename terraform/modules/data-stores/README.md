# data-stores

Creates the Firestore database Henchmen uses for task state, merge queue records and operative reports (plus the composite indexes their hot query paths need), and the two Cloud Storage buckets for dossier artifacts and operative snapshots, with bucket-scoped IAM for the services that use them.

Firestore access is IAM-only. There is deliberately no Firestore Security Rules ruleset: rules are evaluated only for Firebase client-SDK traffic, and every Henchmen component uses the server SDK, which authenticates with IAM and bypasses rules entirely.

Delete protection (Firestore) and `force_destroy = false` (buckets) apply only in `prod`, so dev and staging can be torn down with `terraform destroy`.

## Usage

```hcl
module "data_stores" {
  source                  = "../../modules/data-stores"
  project_id              = var.project_id
  region                  = var.region
  environment             = var.environment
  labels                  = local.labels
  service_account_emails  = module.iam.service_account_emails
  artifact_retention_days = var.artifact_retention_days
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region for Firestore and Cloud Storage resources. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| labels | map(string) | `{}` | Labels to apply to the storage buckets. |
| service_account_emails | map(string) | (required) | Service account emails from the iam module. Must contain `mastermind`, `operative`, `forge`. |
| artifact_retention_days | number | `90` | Days after which dossier artifacts and snapshots are deleted. Must be > 0. |

## Outputs

| Name | Description |
|---|---|
| database_name | The name of the Firestore database (`HENCHMEN_FIRESTORE_DATABASE`). |
| database_id | The ID of the Firestore database. |
| dossier_bucket_name | The dossier artifact bucket (`HENCHMEN_GCS_BUCKET_DOSSIER`). |
| snapshots_bucket_name | The operative snapshot bucket (`HENCHMEN_GCS_BUCKET_SNAPSHOTS`). |

## Resources created

- `google_firestore_database.henchmen` — Named Firestore Native database (`henchmen-${environment}`).
- `google_firestore_index.tasks_status_created_at` — tasks by (status ASC, created_at DESC).
- `google_firestore_index.tasks_source_status` — tasks by (source ASC, status ASC).
- `google_firestore_index.merge_queue_status_created_at` — merge_queue by (status ASC, created_at ASC).
- `google_firestore_index.operative_reports_task_id_completed_at` — operative_reports by (task_id ASC, completed_at DESC).
- `google_storage_bucket.dossier` — `${project_id}-henchmen-${environment}-dossier`, uniform access, public access prevention enforced, lifecycle delete after `artifact_retention_days`.
- `google_storage_bucket.snapshots` — `${project_id}-henchmen-${environment}-snapshots`, same settings.
- `google_storage_bucket_iam_member` — dossier: Mastermind `objectAdmin`, Operative and Forge `objectViewer`; snapshots: Mastermind and Operative `objectAdmin`.
