# henchmen / terraform / environments / staging

Thin entry point for the Henchmen `staging` environment. All the actual
module composition lives in `../root` — this directory just wires the root
module to the staging-specific backend bucket and staging-specific variable
values.

## Layout

- `backend.tf` — GCS backend pointing at `henchmen-tfstate-staging`.
- `main.tf` — single `module "henchmen"` block sourced from `../root`.
- `variables.tf` — variable declarations (forwarded to the root module).
- `outputs.tf` — outputs passed through from the root module.
- `staging.auto.tfvars` — staging values (prod-shaped, scheduler on, larger lairs).

## Init / apply

```bash
cd terraform/environments/staging

terraform init -backend-config=bucket=henchmen-tfstate-staging

terraform apply \
  -var="project_id=YOUR_GCP_PROJECT" \
  -var="github_owner=YOUR_GITHUB_ORG" \
  -var="github_default_repo=OWNER/TARGET_REPO"
```

The `staging.auto.tfvars` file is loaded automatically by terraform, so the
scheduler toggle, lair sizing, and allowlist values don't need to be passed
on the command line.

## Why the split

`dev/` and `staging/` used to be byte-identical copy-pastes of each other.
The root module in `../root` is now the single source of truth for how
modules are composed; each environment directory only carries its backend
config and its tfvars. Any change to the module graph goes into `../root`
and is picked up automatically by both environments on the next apply.
