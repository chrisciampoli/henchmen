# Staging environment values. Prod-shaped by design:
#   - periodic scheduler jobs enabled (watchdog, merge queue, cleanup)
#   - larger lair containers to reflect realistic workloads
#   - cloud build triggers may be opted into once the GitHub repo connection
#     is created in the GCP Console
#
# Project / GitHub identity must still be supplied via CLI or a non-committed
# tfvars file, e.g.:
#   terraform apply \
#     -var="project_id=my-gcp-project" \
#     -var="github_owner=my-org" \
#     -var="github_default_repo=my-org/my-target-repo"

environment = "staging"
region      = "us-central1"
github_repo = "henchmen"

# Lair sizing — larger, prod-shaped.
lair_cpu    = "4"
lair_memory = "8Gi"

# Enable periodic Cloud Scheduler jobs (stale task cleanup, merge queue
# processor, watchdog, DLQ check) so staging exercises them on the same
# cadence as prod.
scheduler_enabled = true

# Cloud Build triggers are off by default. Flip to true once the GitHub
# repository connection has been created manually in the GCP Console
# (Settings > Repositories).
enable_cloud_build = false

# No extra egress allowlist in staging beyond the defaults in the networking
# module. Override here if a third-party webhook needs to be reached.
allowlist_cidrs = []
