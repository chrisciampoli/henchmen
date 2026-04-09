output "agent_id" {
  description = "Placeholder Mastermind agent ID (Agent Engine not yet in Terraform provider)"
  value       = local.agent_id
}

output "gemini_flash_endpoint" {
  description = "Vertex AI endpoint path for Gemini Flash model"
  value       = local.gemini_endpoints["flash"]
}

output "gemini_pro_endpoint" {
  description = "Vertex AI endpoint path for Gemini Pro model"
  value       = local.gemini_endpoints["pro"]
}
