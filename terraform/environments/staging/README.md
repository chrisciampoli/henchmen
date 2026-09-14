# henchmen / terraform / environments / staging

Thin entry point for the Henchmen `staging` environment. All the actual
module composition lives in `../root` — this directory just wires the root
module to a GCS state backend and staging-specific variable values.

## Layout

- `backend.tf` — GCS backend; the bucket name is supplied at `init` time.
- `main.tf` — single `module "henchmen"` block sourced from `../root`.
- `variables.tf` — variable declarations (forwarded to the root module).
- `outputs.tf` — outputs passed through from the root module.
- `staging.auto.tfvars` — committed staging values (prod-shaped: scheduler on, larger lairs, longer retention).
- `staging.auto.tfvars.example` — template for your own `terraform.tfvars` (project and GitHub identity, plus optional knobs).
- `.terraform.lock.hcl` — committed provider lock, so every checkout uses the same `hashicorp/google` release.

## Init / apply

```bash
cd terraform/environments/staging

# Identity values: project_id, github_owner, github_default_repo.
# terraform.tfvars is git-ignored and auto-loaded; do not put these in
# staging.auto.tfvars, which is committed.
cp staging.auto.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

# State bucket names are globally unique, so include the project ID.
gcloud storage buckets create gs://henchmen-tfstate-$PROJECT_ID-staging \
  --location=us-central1 --uniform-bucket-level-access
terraform init -backend-config=bucket=henchmen-tfstate-$PROJECT_ID-staging

terraform plan -out=staging.tfplan
terraform apply staging.tfplan
```

Staging serves `/metrics` only with a real bearer token: populate
`henchmen-staging-metrics-auth-token` before relying on it. After the real
secret values are in Secret Manager, set `seed_secret_placeholders = false` so
a later apply cannot shadow them with a new placeholder version.

Terraform owns the full environment of each Cloud Run service. Values set by
hand with `gcloud run services update` are removed by the next apply; add them
to the `cloud-run-services` module instead.

## Why the split

`dev/` and `staging/` used to be byte-identical copy-pastes of each other.
The root module in `../root` is now the single source of truth for how
modules are composed; each environment directory only carries its backend
config and its tfvars. Any change to the module graph goes into `../root`
and is picked up automatically by both environments on the next apply.
