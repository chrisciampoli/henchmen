# Outputs return `null` (not an empty string) when enable_cloud_build = false.
# Downstream consumers should treat a null value as "trigger not provisioned"
# rather than "trigger provisioned with an empty ID".

output "pr_trigger_id" {
  description = "The ID of the PR CI Cloud Build trigger, or null when cloud build is disabled"
  value       = try(google_cloudbuild_trigger.pr_ci[0].id, null)
}

output "operative_build_trigger_id" {
  description = "The ID of the operative image build Cloud Build trigger, or null when cloud build is disabled"
  value       = try(google_cloudbuild_trigger.operative_image[0].id, null)
}
