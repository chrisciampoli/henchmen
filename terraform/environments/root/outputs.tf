# Networking
output "vpc_id" {
  description = "The ID of the Henchmen VPC network"
  value       = module.networking.vpc_id
}

output "vpc_name" {
  description = "The name of the Henchmen VPC network"
  value       = module.networking.vpc_name
}

output "subnet_id" {
  description = "The ID of the primary subnet"
  value       = module.networking.subnet_id
}

output "connector_id" {
  description = "The ID of the VPC Serverless Access Connector"
  value       = module.networking.connector_id
}

# IAM
output "service_account_emails" {
  description = "Map of service name to service account email"
  value       = module.iam.service_account_emails
}

# Secrets
output "secret_ids" {
  description = "Map of logical secret name to Secret Manager secret ID"
  value       = module.secrets.secret_ids
}

# Data stores
output "database_name" {
  description = "The name of the Firestore database"
  value       = module.data_stores.database_name
}

output "database_id" {
  description = "The ID of the Firestore database"
  value       = module.data_stores.database_id
}

output "dossier_bucket_name" {
  description = "The GCS bucket holding dossier artifacts (HENCHMEN_GCS_BUCKET_DOSSIER)"
  value       = module.data_stores.dossier_bucket_name
}

output "snapshots_bucket_name" {
  description = "The GCS bucket holding operative snapshots (HENCHMEN_GCS_BUCKET_SNAPSHOTS)"
  value       = module.data_stores.snapshots_bucket_name
}

# Pub/Sub
output "topic_ids" {
  description = "Map of logical topic name to Pub/Sub topic ID"
  value       = module.pubsub.topic_ids
}

output "subscription_ids" {
  description = "Map of logical subscription name to Pub/Sub subscription ID"
  value       = module.pubsub.subscription_ids
}

# Artifact Registry
output "repository_id" {
  description = "The Artifact Registry repository ID"
  value       = module.artifact_registry.repository_id
}

output "repository_url" {
  description = "The Docker push/pull URL for the Artifact Registry repository"
  value       = module.artifact_registry.repository_url
}

# Cloud Run services
output "service_urls" {
  description = "Map of component name to Cloud Run service URL"
  value       = module.cloud_run_services.service_urls
}

output "service_names" {
  description = "Map of component name to Cloud Run service name"
  value       = module.cloud_run_services.service_names
}

output "pubsub_audiences" {
  description = "Map of component name to the OIDC audience the service verifies (HENCHMEN_PUBSUB_OIDC_AUDIENCE)"
  value       = module.cloud_run_services.pubsub_audiences
}

# Cloud Run lairs
output "lair_template_job_name" {
  description = "The name of the Operative Lair template Cloud Run Job"
  value       = module.cloud_run_lairs.job_name
}

output "lair_template_job_id" {
  description = "The resource ID of the Operative Lair template Cloud Run Job"
  value       = module.cloud_run_lairs.job_id
}

# Cloud Build
output "pr_trigger_id" {
  description = "The ID of the PR CI Cloud Build trigger (null when cloud build is disabled)"
  value       = module.cloud_build.pr_trigger_id
}

output "operative_build_trigger_id" {
  description = "The ID of the operative image build Cloud Build trigger (null when cloud build is disabled)"
  value       = module.cloud_build.operative_build_trigger_id
}

# Observability
output "log_sink_name" {
  description = "The name of the Henchmen log sink"
  value       = module.observability.log_sink_name
}

output "log_bucket_id" {
  description = "The resource ID of the log bucket the sink writes to"
  value       = module.observability.log_bucket_id
}

output "dashboard_id" {
  description = "The resource name of the Henchmen monitoring dashboard"
  value       = module.observability.dashboard_id
}

output "alert_policy_ids" {
  description = "Map of alert policy name to resource name"
  value       = module.observability.alert_policy_ids
}

# Scheduler (null when scheduler_enabled = false)
output "cleanup_job_name" {
  description = "The name of the stale-task-cleanup Cloud Scheduler job"
  value       = try(module.scheduler[0].cleanup_job_name, null)
}

output "merge_queue_job_name" {
  description = "The name of the merge-queue-processor Cloud Scheduler job"
  value       = try(module.scheduler[0].merge_queue_job_name, null)
}

output "watchdog_job_name" {
  description = "The name of the watchdog Cloud Scheduler job"
  value       = try(module.scheduler[0].watchdog_job_name, null)
}

output "dlq_check_job_name" {
  description = "The name of the DLQ check Cloud Scheduler job"
  value       = try(module.scheduler[0].dlq_check_job_name, null)
}
