# secrets

Provisions the Secret Manager secrets Henchmen uses for inbound integrations (GitHub, Slack bot/signing/app tokens, Jira) and grants per-service-account accessor IAM on the ones each component needs. The module creates empty secret containers only; operators must populate the actual secret versions after the initial apply (see `gcloud secrets versions add`). The Slack app token is seeded with a placeholder so Dispatch can boot in Socket Mode during first deploy.

## Usage

```hcl
module "secrets" {
  source                 = "../../modules/secrets"
  project_id             = var.project_id
  environment            = var.environment
  service_account_emails = module.iam.service_account_emails
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| service_account_emails | map(string) | (required) | Map of service name to service account email (from the iam module outputs). |
| labels | map(string) | `{}` | Labels to apply to Secret Manager secrets. |

## Outputs

| Name | Description |
|---|---|
| github_token_secret_id | Secret Manager secret ID for the GitHub token. |
| slack_bot_token_secret_id | Secret Manager secret ID for the Slack bot token. |
| slack_signing_secret_id | Secret Manager secret ID for the Slack signing secret. |
| slack_app_token_secret_id | Secret Manager secret ID for the Slack app token (Socket Mode). |
| jira_api_token_secret_id | Secret Manager secret ID for the Jira API token. |
| secret_ids | Map of logical secret name to Secret Manager secret ID. |

## Resources created

- `google_secret_manager_secret` — Five secrets: github-token, slack-bot-token, slack-signing-secret, slack-app-token, jira-api-token.
- `google_secret_manager_secret_version.slack_app_token_placeholder` — Placeholder version so Dispatch can start before real token is rotated in.
- `google_secret_manager_secret_iam_member` — Per-secret accessor bindings for Mastermind, Dispatch, Operative, Forge, and Dossier as needed.
