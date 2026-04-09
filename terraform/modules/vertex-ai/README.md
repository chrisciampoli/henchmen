# vertex-ai

Records the Gemini model endpoint metadata Mastermind needs at runtime. This module creates no GCP resources today — Gemini is accessed via the Vertex AI API by service-account identity, and the Vertex AI Agent Engine resource type is not yet exposed by the `google` Terraform provider. Its outputs exist so other modules and root configurations can reference a single source of truth for which Gemini endpoints are in use. When the provider gains Agent Engine support, the actual `google_vertex_ai_*` resources should be added here.

## Usage

```hcl
module "vertex_ai" {
  source              = "../../modules/vertex-ai"
  project_id          = var.project_id
  region              = var.region
  environment         = var.environment
  mastermind_sa_email = module.iam.mastermind_sa_email
}
```

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| project_id | string | (required) | The GCP project ID. |
| region | string | (required) | The GCP region for Vertex AI resources. |
| environment | string | (required) | The deployment environment (e.g. dev, staging, prod). |
| labels | map(string) | `{}` | Labels to apply to Vertex AI resources. |
| mastermind_sa_email | string | (required) | Service account email for Mastermind (used for Agent Engine access). |

## Outputs

| Name | Description |
|---|---|
| agent_id | Placeholder Mastermind agent ID (Agent Engine not yet in Terraform provider). |
| gemini_flash_endpoint | Vertex AI endpoint path for the Gemini Flash model. |
| gemini_pro_endpoint | Vertex AI endpoint path for the Gemini Pro model. |

## Resources created

- None. Gemini endpoints are recorded only in `locals` until Terraform gains provider support for Vertex AI Agent Engine resources.
