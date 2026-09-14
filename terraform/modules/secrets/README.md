# secrets

Provisions the Secret Manager secrets Henchmen mounts into its Cloud Run services and lairs (GitHub token, Slack bot / signing / app tokens, Jira API token, the `/metrics` bearer token, and the Dispatch `/api/v1/tasks` bearer token) and grants per-service-account accessor IAM on the ones each component mounts.

Dispatch treats the seeded placeholder value as "no token": `POST /api/v1/tasks` returns 401 in staging and prod until a real `dispatch-api-token` version is added. Callers send it as `Authorization: Bearer <token>`; when `dispatch_public_ingress = false` Cloud Run IAM also needs a Google identity token, which then goes in `X-Serverless-Authorization` so the `Authorization` header reaches Dispatch.

Cloud Run refuses to start a revision that mounts a secret with no version, so when `seed_secret_placeholders = true` (the default) every secret gets a placeholder version on first apply. Placeholders are not usable credentials — they fail at the first API call. Dispatch logs the failed Slack Socket Mode connection and keeps serving HTTP rather than crash-looping. Add the real value as a new version, which becomes `latest`:

```bash
printf '%s' "$GITHUB_TOKEN" | gcloud secrets versions add henchmen-dev-github-token --data-file=-
```

On a project whose secrets already hold real values, set `seed_secret_placeholders = false` before applying: a newly created placeholder would become `latest` and shadow them. A secret added to this module later (such as `dispatch-api-token`) then has no version, so create it and add a version before the apply that mounts it.

## Usage

```hcl
module "secrets" {
  source                   = "../../modules/secrets"
  project_id               = var.project_id
  environment              = var.environment
  labels                   = local.labels
  service_account_emails   = module.iam.service_account_emails
  seed_secret_placeholders = var.seed_secret_placeholders
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| service_account_emails | map(string) | (required) | Service account emails from the iam module. Must contain `mastermind`, `dispatch`, `operative`, `forge`. |
| labels | map(string) | `{}` | Labels to apply to Secret Manager secrets. |
| seed_secret_placeholders | bool | `true` | Create a placeholder version for every secret so the first apply produces startable revisions. |

## Outputs

| Name | Description |
|---|---|
| github_token_secret_id | Secret ID for the GitHub token. |
| slack_bot_token_secret_id | Secret ID for the Slack bot token. |
| slack_signing_secret_id | Secret ID for the Slack signing secret. |
| slack_app_token_secret_id | Secret ID for the Slack app token (Socket Mode). |
| jira_api_token_secret_id | Secret ID for the Jira API token. |
| metrics_auth_token_secret_id | Secret ID for the `/metrics` bearer token. |
| dispatch_api_token_secret_id | Secret ID for the Dispatch `/api/v1/tasks` bearer token. |
| secret_ids | Map of logical secret name to secret ID. |

## Resources created

- `google_secret_manager_secret` — Seven secrets: github-token, slack-bot-token, slack-signing-secret, slack-app-token, jira-api-token, metrics-auth-token, dispatch-api-token (all `henchmen-${environment}-*`).
- `google_secret_manager_secret_version.placeholder` — One placeholder per secret when `seed_secret_placeholders = true`; `secret_data` changes are ignored.
- `google_secret_manager_secret_iam_member` — github-token: Mastermind, Operative, Forge. slack-bot-token: Dispatch, Mastermind. slack-signing-secret, slack-app-token, jira-api-token, dispatch-api-token: Dispatch. metrics-auth-token: Mastermind, Dispatch, Forge.
