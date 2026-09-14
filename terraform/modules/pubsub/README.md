# pubsub

The Pub/Sub topic-and-subscription fabric Henchmen uses as its async control plane. Only topics that a component actually publishes to exist. Push subscriptions authenticate with an OIDC token minted for the dedicated push service account, and the token `audience` is set explicitly on every one to the value the receiving service registers via `custom_audiences` and verifies as `HENCHMEN_PUBSUB_OIDC_AUDIENCE` — a mismatch rejects every push with 401.

Every source subscription dead-letters to a shared topic after 5 attempts. The Pub/Sub service agent is granted publisher on the dead-letter topic and subscriber on each source subscription; without those grants the forward silently fails and the message is dropped instead of dead-lettered.

## Usage

```hcl
module "pubsub" {
  source      = "../../modules/pubsub"
  project_id  = var.project_id
  environment = var.environment
  labels      = local.labels

  push_endpoints = {
    mastermind_url = module.cloud_run_services.service_urls["mastermind"]
    dispatch_url   = module.cloud_run_services.service_urls["dispatch"]
    forge_url      = module.cloud_run_services.service_urls["forge"]
  }
  push_audiences = {
    mastermind = module.cloud_run_services.pubsub_audiences["mastermind"]
    dispatch   = module.cloud_run_services.pubsub_audiences["dispatch"]
    forge      = module.cloud_run_services.pubsub_audiences["forge"]
  }
  push_sa_email                    = module.iam.service_account_emails["pubsub_push"]
  enable_cloud_build_notifications = var.enable_cloud_build_notifications
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| labels | map(string) | `{}` | Labels to apply to Pub/Sub topics. |
| push_endpoints | object | (required) | Service URLs for push subscriptions (`mastermind_url`, `dispatch_url`, `forge_url`). |
| push_audiences | object | (required) | OIDC audience per receiving service (`mastermind`, `dispatch`, `forge`). |
| push_sa_email | string | (required) | Service account Pub/Sub mints push OIDC tokens for. |
| enable_cloud_build_notifications | bool | `false` | Subscribe Forge to the project's `cloud-builds` topic. That topic only exists after the first Cloud Build run. |

## Outputs

| Name | Description |
|---|---|
| topic_ids | Map of logical topic name to Pub/Sub topic ID. |
| subscription_ids | Map of logical subscription name to Pub/Sub subscription ID (`build_complete` is `null` when disabled). |

## Resources created

- `google_pubsub_topic` — 7 topics, all `henchmen-${environment}-*`: task-intake, operative-complete, forge-request, forge-result, ci-failure, embed-request, dead-letter.
- `google_pubsub_subscription` — Push subscriptions task-intake, operative-complete, forge-result and ci-failure (to Mastermind) and forge-request (to Forge); a pull subscription on dead-letter with exactly-once delivery; and, when enabled, build-complete from `cloud-builds` to Forge. embed-request has no subscription yet — the topic exists so the GitHub webhook publish does not fail.
- `google_pubsub_topic_iam_member.dead_letter_publisher` / `google_pubsub_subscription_iam_member.dead_letter_subscriber` — Service agent grants that make dead-lettering work.
