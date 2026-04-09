# pubsub

Provisions the Pub/Sub topic-and-subscription fabric Henchmen uses as its async control plane: task intake, task planning, operative dispatch/status/complete, forge request/result, CI failure, and a shared dead-letter topic. Push subscriptions that target Cloud Run services (Mastermind, Dispatch, Forge) are configured with OIDC tokens so the push service account can invoke the target — forgetting the `audience` here causes silent 403s, which is why it is set explicitly on every push subscription.

## Usage

```hcl
module "pubsub" {
  source        = "../../modules/pubsub"
  project_id    = var.project_id
  environment   = var.environment
  push_sa_email = module.iam.service_account_emails["mastermind"]
  push_endpoints = {
    mastermind_url = module.cloud_run_services.service_urls["mastermind"]
    dispatch_url   = module.cloud_run_services.service_urls["dispatch"]
    forge_url      = module.cloud_run_services.service_urls["forge"]
  }
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| labels | map(string) | `{}` | Labels to apply to Pub/Sub resources. |
| push_endpoints | object | placeholder URLs | Service URLs for push subscriptions (`mastermind_url`, `dispatch_url`, `forge_url`). |
| push_sa_email | string | `""` | Service account email for Pub/Sub push OIDC authentication. |

## Outputs

| Name | Description |
|---|---|
| topic_ids | Map of logical topic name to Pub/Sub topic ID. |
| subscription_ids | Map of logical subscription name to Pub/Sub subscription ID. |

## Resources created

- `google_pubsub_topic` — 9 topics: task-intake, task-planned, operative-dispatch, operative-status, operative-complete, forge-request, forge-result, ci-failure, dead-letter.
- `google_pubsub_subscription` — 9 subscriptions with dead-letter policies and exponential retry; push subscriptions use OIDC to invoke Cloud Run, pull subscriptions (`operative-dispatch`, `operative-status`, `dead-letter`) use exactly-once delivery where needed.
