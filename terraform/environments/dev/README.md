# henchmen / terraform / environments / dev

Thin entry point for the Henchmen `dev` environment. All the actual module
composition lives in `../root` — this directory just wires the root module
to the dev-specific backend bucket and dev-specific variable values.

## Layout

- `backend.tf` — GCS backend pointing at `henchmen-tfstate-dev`.
- `main.tf` — single `module "henchmen"` block sourced from `../root`.
- `variables.tf` — variable declarations (forwarded to the root module).
- `outputs.tf` — outputs passed through from the root module.
- `dev.auto.tfvars` — the actual dev values (cheap sizing, no scheduler, no cloud build).

## Init / apply

```bash
cd terraform/environments/dev

terraform init -backend-config=bucket=henchmen-tfstate-dev

terraform apply \
  -var="project_id=YOUR_GCP_PROJECT" \
  -var="github_owner=YOUR_GITHUB_ORG" \
  -var="github_default_repo=OWNER/TARGET_REPO"
```

The `dev.auto.tfvars` file is loaded automatically by terraform, so the
per-environment sizing, scheduler toggle, and allowlist values don't need to
be passed on the command line.

## Why the split

`dev/` and `staging/` used to be byte-identical copy-pastes of each other,
which meant any change to module wiring had to be made twice and drifted in
practice. The root module in `../root` is now the single source of truth for
how modules are composed; each environment directory only carries its backend
config and its tfvars. Adding a new environment (e.g. `prod`) is just a new
directory with a `backend.tf`, a `main.tf` that sources `../root`, and a
`prod.auto.tfvars` with prod-shaped values.
