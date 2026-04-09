# Outputs commented out until Cloud Build triggers are enabled
# (requires GitHub repo connection via GCP Console first)

output "pr_trigger_id" {
  description = "The ID of the PR CI Cloud Build trigger"
  value       = ""
}

output "operative_build_trigger_id" {
  description = "The ID of the operative image build Cloud Build trigger"
  value       = ""
}
