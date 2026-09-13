# Staging environment values. Prod-shaped by design:
#   - periodic scheduler jobs enabled (watchdog, merge queue, cleanup)
#   - larger lair containers to reflect realistic workloads
#   - cloud build triggers may be opted into once the GitHub repo connection
#     is created in the GCP Console
#
# Project and GitHub identity are NOT here: they differ per self-hoster and
# this file is committed. Put them in terraform.tfvars (git-ignored):
#
#   cp staging.auto.tfvars.example terraform.tfvars && $EDITOR terraform.tfvars

environment = "staging"
region      = "us-central1"
github_repo = "henchmen"

# Lair sizing — larger, prod-shaped.
lair_cpu     = "4"
lair_memory  = "8Gi"
lair_timeout = 1800

# Enable periodic Cloud Scheduler jobs (stale task cleanup, merge queue
# processor, watchdog, DLQ check) so staging exercises them on the same
# cadence as prod.
scheduler_enabled = true

# Cloud Build triggers are off by default. Flip to true once the GitHub
# repository connection has been created manually in the GCP Console
# (Settings > Repositories). enable_cloud_build_notifications additionally
# needs the project's `cloud-builds` topic to exist, which the first build
# creates.
enable_cloud_build               = false
enable_cloud_build_notifications = false

# Staging keeps more history than dev.
log_retention_days      = 30
artifact_retention_days = 90
