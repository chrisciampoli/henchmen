# cloud-build

Cloud Build triggers that run PR CI and build the Operative image on push to `main`. Both triggers are gated on `enable_cloud_build` (default `false`): they need a GitHub repository connection that can only be created by hand in the GCP Console (Settings > Repositories > Connect Repository), so with the flag off the module provisions nothing and its outputs are `null`. Once the connection exists, set `enable_cloud_build = true` in the environment tfvars and re-apply.

The PR CI trigger runs the task completion checklist in a single build step (`pip install -e '.[dev]'`, `ruff check`, `ruff format --check`, `mypy src/`, `pytest tests/unit/`) — one step because each Cloud Build step is a fresh container and only `/workspace` persists between steps.

## Usage

```hcl
module "cloud_build" {
  source             = "../../modules/cloud-build"
  project_id         = var.project_id
  region             = var.region
  environment        = var.environment
  github_owner       = var.github_owner
  github_repo        = var.github_repo
  enable_cloud_build = var.enable_cloud_build
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region for Cloud Build triggers. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| github_owner | string | (required) | GitHub organisation or username that owns the repository. |
| github_repo | string | (required) | GitHub repository name (without the owner prefix). |
| enable_cloud_build | bool | `false` | Provision the triggers. Requires the manual GitHub repository connection first. |

## Outputs

| Name | Description |
|---|---|
| pr_trigger_id | The ID of the PR CI trigger, or `null` when `enable_cloud_build = false`. |
| operative_build_trigger_id | The ID of the operative image build trigger, or `null` when `enable_cloud_build = false`. |

## Resources created

Only when `enable_cloud_build = true`:

- `google_cloudbuild_trigger.pr_ci` — Runs lint, format check, type check and unit tests on every pull request.
- `google_cloudbuild_trigger.operative_image` — Builds and pushes `operative:$SHORT_SHA` and `operative:latest` on push to `main`.
