# data-stores

Creates the Firestore database Henchmen uses for task state, merge queue records, and operative reports, plus the composite indexes those collections need for their hot query paths. Delete protection is enabled only in `prod` so dev/staging can be torn down cleanly by `terraform destroy`.

## Usage

```hcl
module "data_stores" {
  source      = "../../modules/data-stores"
  project_id  = var.project_id
  region      = var.region
  environment = var.environment
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region for Firestore resources. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |

## Outputs

| Name | Description |
|---|---|
| database_name | The name of the Firestore database. |
| database_id | The ID of the Firestore database. |

## Resources created

- `google_firestore_database.henchmen` — Named Firestore Native database (`henchmen-${environment}`).
- `google_firestore_index.tasks_status_created_at` — Composite index: tasks by (status ASC, created_at DESC).
- `google_firestore_index.tasks_source_status` — Composite index: tasks by (source ASC, status ASC).
- `google_firestore_index.merge_queue_status_created_at` — Composite index: merge_queue by (status ASC, created_at ASC).
- `google_firestore_index.operative_reports_task_id_completed_at` — Composite index: operative_reports by (task_id ASC, completed_at DESC).
