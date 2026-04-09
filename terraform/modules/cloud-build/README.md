# cloud-build

Placeholder module for Cloud Build triggers that run PR CI and build the Operative image on push to main. All trigger resources are currently commented out because they require a GitHub repository connection to be set up manually via the GCP Console (Settings > Repositories) before Terraform can reference them. Once connected, uncomment the resources in `main.tf` and re-apply.

## Usage

```hcl
module "cloud_build" {
  source       = "../../modules/cloud-build"
  project_id   = var.project_id
  region       = var.region
  environment  = var.environment
  github_owner = var.github_owner
  github_repo  = var.github_repo
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region for Cloud Build triggers. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| labels | map(string) | `{}` | Labels to apply to Cloud Build resources. |
| github_owner | string | (required) | GitHub organisation or username that owns the repository. |
| github_repo | string | (required) | GitHub repository name (without the owner prefix). |

## Outputs

| Name | Description |
|---|---|
| pr_trigger_id | The ID of the PR CI Cloud Build trigger (empty until trigger is enabled). |
| operative_build_trigger_id | The ID of the operative image build Cloud Build trigger (empty until trigger is enabled). |

## Resources created

- None currently. The `google_cloudbuild_trigger.pr_ci` and `google_cloudbuild_trigger.operative_image` resources are defined but commented out pending a GitHub repo connection.
