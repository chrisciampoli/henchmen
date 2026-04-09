# All outputs bubble up from the root module. Adding or removing an output
# should happen in ../root/outputs.tf, not here.

output "vpc_id" {
  description = "The ID of the Henchmen VPC network"
  value       = module.henchmen.vpc_id
}

output "vpc_name" {
  description = "The name of the Henchmen VPC network"
  value       = module.henchmen.vpc_name
}

output "subnet_id" {
  description = "The ID of the primary subnet"
  value       = module.henchmen.subnet_id
}

output "connector_id" {
  description = "The ID of the VPC Serverless Access Connector"
  value       = module.henchmen.connector_id
}

output "service_account_emails" {
  description = "Map of service name to service account email"
  value       = module.henchmen.service_account_emails
}

output "secret_ids" {
  description = "Map of logical secret name to Secret Manager secret ID"
  value       = module.henchmen.secret_ids
}

output "database_name" {
  description = "The name of the Firestore database"
  value       = module.henchmen.database_name
}

output "database_id" {
  description = "The ID of the Firestore database"
  value       = module.henchmen.database_id
}

output "topic_ids" {
  description = "Map of logical topic name to Pub/Sub topic ID"
  value       = module.henchmen.topic_ids
}

output "subscription_ids" {
  description = "Map of logical subscription name to Pub/Sub subscription ID"
  value       = module.henchmen.subscription_ids
}

output "repository_id" {
  description = "The Artifact Registry repository ID"
  value       = module.henchmen.repository_id
}

output "repository_url" {
  description = "The Docker push/pull URL for the Artifact Registry repository"
  value       = module.henchmen.repository_url
}

output "service_urls" {
  description = "Map of component name to Cloud Run service URL"
  value       = module.henchmen.service_urls
}

output "service_names" {
  description = "Map of component name to fully-qualified Cloud Run service resource name"
  value       = module.henchmen.service_names
}

output "lair_template_job_name" {
  description = "The name of the Operative Lair template Cloud Run Job"
  value       = module.henchmen.lair_template_job_name
}

output "lair_template_job_id" {
  description = "The resource ID of the Operative Lair template Cloud Run Job"
  value       = module.henchmen.lair_template_job_id
}

output "pr_trigger_id" {
  description = "The ID of the PR CI Cloud Build trigger (null when disabled)"
  value       = module.henchmen.pr_trigger_id
}

output "operative_build_trigger_id" {
  description = "The ID of the operative image build Cloud Build trigger (null when disabled)"
  value       = module.henchmen.operative_build_trigger_id
}

output "log_sink_name" {
  description = "The name of the Henchmen log sink"
  value       = module.henchmen.log_sink_name
}

output "dashboard_id" {
  description = "The resource name of the Henchmen monitoring dashboard"
  value       = module.henchmen.dashboard_id
}

output "alert_policy_ids" {
  description = "Map of alert policy name to resource name"
  value       = module.henchmen.alert_policy_ids
}

output "vertex_ai_agent_id" {
  description = "The resource ID of the Mastermind Vertex AI Agent Engine instance"
  value       = module.henchmen.vertex_ai_agent_id
}

output "cleanup_job_name" {
  description = "The name of the stale-task-cleanup Cloud Scheduler job (null when scheduler disabled)"
  value       = module.henchmen.cleanup_job_name
}

output "merge_queue_job_name" {
  description = "The name of the merge-queue-processor Cloud Scheduler job (null when scheduler disabled)"
  value       = module.henchmen.merge_queue_job_name
}
