# iam

Creates one service account per Henchmen workload (Mastermind, Dispatch, Operative, Forge) plus two caller identities (Pub/Sub push and Cloud Scheduler), and binds each to the roles it needs. Arsenal has no service account because it runs in-process inside the Operative container, and Dossier has none because it is a library that runs inside Mastermind.

Every other module that needs a service account email consumes `service_account_emails`, so this module runs before `secrets`, `data-stores`, `cloud-run-services`, `cloud-run-lairs`, `pubsub` and `scheduler`.

Notes on the bindings:

- No workload identity holds project-level `roles/run.invoker`. Nothing in Henchmen calls another Cloud Run service directly; the Pub/Sub push and Scheduler identities get service-level `run.invoker` in the `cloud-run-services` module. Keeping them separate from Mastermind means a leaked Mastermind token cannot be replayed as a Pub/Sub delivery.
- `roles/aiplatform.user` (Mastermind for the RAG corpus, Operative for the coding models) carries a condition that denies publisher models from any publisher other than Google — the IAM half of the "no Claude on Vertex AI" rule. Non-publisher resources such as RAG corpora stay allowed.
- Mastermind holds `roles/iam.serviceAccountUser` on the Operative account so it can create lair jobs that run as it.
- Bucket-scoped storage roles live in the `data-stores` module; secret accessor bindings live in the `secrets` module.

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
| forge_sa_email | Email of the Forge service account. |
| forge_sa_id | ID of the Forge service account. |
| pubsub_push_sa_email | Email of the Pub/Sub push (OIDC caller) service account. |
| scheduler_sa_email | Email of the Cloud Scheduler (OIDC caller) service account. |
| service_account_emails | Map with keys `mastermind`, `dispatch`, `operative`, `forge`, `pubsub_push`, `scheduler`. |

## Resources created

- `google_service_account` — Six accounts: `sa-${environment}-mastermind`, `-dispatch`, `-operative`, `-forge`, `-pubsub-push`, `-scheduler`.
- `google_project_iam_member.mastermind` — run.developer, pubsub.publisher, pubsub.subscriber, datastore.user, cloudtrace.agent.
- `google_project_iam_member.dispatch` — pubsub.publisher, cloudtrace.agent.
- `google_project_iam_member.operative` — pubsub.publisher, datastore.user, cloudtrace.agent.
- `google_project_iam_member.forge` — cloudbuild.builds.editor, pubsub.publisher, pubsub.subscriber, datastore.user, cloudtrace.agent.
- `google_project_iam_member.aiplatform_google_publishers_only` — Conditioned aiplatform.user for Mastermind and Operative.
- `google_service_account_iam_member.mastermind_actas_operative` — serviceAccountUser on the Operative account for Mastermind.
