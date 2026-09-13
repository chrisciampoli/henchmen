# Dev environment values. Cheap by design:
#   - no scheduled jobs (watchdog, merge queue, cleanup)
#   - small lair containers
#   - no cloud build triggers (manual docker push instead)
#   - short log / artifact retention
#
# Project and GitHub identity are NOT here: they differ per self-hoster and
# this file is committed. Put them in terraform.tfvars (git-ignored):
#
#   cp dev.auto.tfvars.example terraform.tfvars && $EDITOR terraform.tfvars

environment = "dev"
region      = "us-central1"
github_repo = "henchmen"

# Lair sizing — small, cheap, fine for one-at-a-time dev runs.
lair_cpu     = "2"
lair_memory  = "4Gi"
lair_timeout = 1800

# No periodic scheduler jobs in dev (watchdog, DLQ check, cleanup, merge queue).
# Run them ad-hoc via `curl` during debugging instead.
scheduler_enabled = false

# No Cloud Build triggers in dev. Images are built locally and pushed manually.
enable_cloud_build               = false
enable_cloud_build_notifications = false

# Keep dev cheap: logs and artifacts expire quickly.
log_retention_days      = 14
artifact_retention_days = 30
