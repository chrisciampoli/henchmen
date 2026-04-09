# Dev environment values. Cheap by design:
#   - no scheduled jobs (watchdog, merge queue, cleanup)
#   - small lair containers
#   - no cloud build triggers (manual docker push instead)
#
# Project / GitHub identity must still be supplied via CLI or a non-committed
# tfvars file, e.g.:
#   terraform apply \
#     -var="project_id=my-gcp-project" \
#     -var="github_owner=my-org" \
#     -var="github_default_repo=my-org/my-target-repo"

environment = "dev"
region      = "us-central1"
github_repo = "henchmen"

# Lair sizing — small, cheap, fine for one-at-a-time dev runs.
lair_cpu    = "2"
lair_memory = "4Gi"

# No periodic scheduler jobs in dev (watchdog, DLQ check, cleanup, merge queue).
# Run them ad-hoc via `curl` during debugging instead.
scheduler_enabled = false

# No Cloud Build triggers in dev. Images are built locally and pushed manually.
enable_cloud_build = false

# Dev has no extra allowlisted egress beyond the defaults in the networking module.
allowlist_cidrs = []
