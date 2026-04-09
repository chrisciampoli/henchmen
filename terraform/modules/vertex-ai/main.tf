# ---------------------------------------------------------------------------
# Vertex AI — Gemini endpoint configuration
#
# Note: Gemini models are accessed via the Vertex AI API and require no
# resource provisioning. This module records model endpoint metadata in
# locals for reference by other parts of the infrastructure.
#
# The Vertex AI Agent Engine resource type is not yet available in the
# google Terraform provider. When it becomes available, add the resource
# here. For now, Mastermind uses the Vertex AI API directly at runtime.
# ---------------------------------------------------------------------------

locals {
  # Gemini model endpoints — referenced by Mastermind at runtime via env vars.
  # These are API paths, not provisioned resources.
  gemini_endpoints = {
    flash = "projects/${var.project_id}/locations/${var.region}/publishers/google/models/gemini-2.0-flash-001"
    pro   = "projects/${var.project_id}/locations/${var.region}/publishers/google/models/gemini-2.5-pro-preview-05-06"
  }

  # Placeholder agent ID until the provider supports Agent Engine resources
  agent_id = "henchmen-${var.environment}-mastermind"
}
