# iam

Creates one least-privilege service account per Henchmen component (Mastermind, Dispatch, Operative, Arsenal, Forge, Dossier) and binds each to the specific set of project-level roles it needs. Every other module that needs a service account email consumes `service_account_emails` from this module's outputs, so this module must run before `cloud-run-services`, `cloud-run-lairs`, `secrets`, and `scheduler`.

## Usage

```hcl
module "iam" {
  source      = "../../modules/iam"
  project_id  = var.project_id
  environment = var.environment
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |

## Outputs

| Name | Description |
|---|---|
| mastermind_sa_email | Email of the Mastermind service account. |
| mastermind_sa_id | ID of the Mastermind service account. |
| dispatch_sa_email | Email of the Dispatch service account. |
| dispatch_sa_id | ID of the Dispatch service account. |
| operative_sa_email | Email of the Operative service account. |
| operative_sa_id | ID of the Operative service account. |
| arsenal_sa_email | Email of the Arsenal service account. |
| arsenal_sa_id | ID of the Arsenal service account. |
| forge_sa_email | Email of the Forge service account. |
| forge_sa_id | ID of the Forge service account. |
| dossier_sa_email | Email of the Dossier service account. |
| dossier_sa_id | ID of the Dossier service account. |
| service_account_emails | Map of component name to service account email. |

## Resources created

- `google_service_account` — Six service accounts: mastermind, dispatch, operative, arsenal, forge, dossier.
- `google_project_iam_member` — Project-level role bindings tailored per component (run.invoker, pubsub.publisher/subscriber, datastore.user, aiplatform.user, storage.objectAdmin, cloudtrace.agent, etc.).
