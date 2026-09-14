# henchmen / terraform / environments / dev

Thin entry point for the Henchmen `dev` environment. All the actual module
composition lives in `../root` — this directory just wires the root module
to a GCS state backend and dev-specific variable values.

## Layout

- `backend.tf` — GCS backend; the bucket name is supplied at `init` time.
- `main.tf` — single `module "henchmen"` block sourced from `../root`.
- `variables.tf` — variable declarations (forwarded to the root module).
- `outputs.tf` — outputs passed through from the root module.
- `dev.auto.tfvars` — committed dev sizing (small lairs, no scheduler, no Cloud Build, short retention).
- `dev.auto.tfvars.example` — template for your own `terraform.tfvars` (project and GitHub identity, plus optional knobs).
- `.terraform.lock.hcl` — committed provider lock, so every checkout uses the same `hashicorp/google` release.

## Init / apply

```bash
cd terraform/environments/dev

# Identity values: project_id, github_owner, github_default_repo.
# terraform.tfvars is git-ignored and auto-loaded; do not put these in
# dev.auto.tfvars, which is committed.
cp dev.auto.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

# State bucket names are globally unique, so include the project ID.
gcloud storage buckets create gs://henchmen-tfstate-$PROJECT_ID-dev \
  --location=us-central1 --uniform-bucket-level-access
terraform init -backend-config=bucket=henchmen-tfstate-$PROJECT_ID-dev

terraform plan -out=dev.tfplan
terraform apply dev.tfplan
```

The first apply deploys a public placeholder image to every service and seeds
every secret with a placeholder version, so it converges on a fresh project.
Then:

1. Add real secret values: `gcloud secrets versions add henchmen-dev-<name> --data-file=-`.
2. Set `seed_secret_placeholders = false` in `terraform.tfvars`.
3. Build and push the mastermind, dispatch, forge and operative images, set
   `container_image_tag`, and apply again.

Terraform owns the full environment of each Cloud Run service. Values set by
hand with `gcloud run services update` are removed by the next apply; add them
to the `cloud-run-services` module instead.

## Why the split

`dev/` and `staging/` used to be byte-identical copy-pastes of each other,
which meant any change to module wiring had to be made twice and drifted in
practice. The root module in `../root` is now the single source of truth for
how modules are composed; each environment directory only carries its backend
config and its tfvars. Adding a new environment (e.g. `prod`) is just a new
directory with a `backend.tf`, a `main.tf` that sources `../root`, and a
`prod.auto.tfvars` with prod-shaped values.
