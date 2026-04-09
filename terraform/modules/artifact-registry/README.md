# artifact-registry

Creates the single Docker-format Artifact Registry repository Henchmen publishes every container image into (mastermind, dispatch, forge, operative, arsenal). The repository name is environment-scoped so dev/staging/prod push to isolated registries.

## Usage

```hcl
module "artifact_registry" {
  source      = "../../modules/artifact-registry"
  project_id  = var.project_id
  region      = var.region
  environment = var.environment
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region for the Artifact Registry repository. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| labels | map(string) | `{}` | Labels to apply to the Artifact Registry repository. |

## Outputs

| Name | Description |
|---|---|
| repository_id | The Artifact Registry repository ID. |
| repository_url | The Docker pull/push URL for the repository (`REGION-docker.pkg.dev/PROJECT/REPO`). |

## Resources created

- `google_artifact_registry_repository.henchmen` — Docker-format regional registry named `henchmen-${environment}`.
