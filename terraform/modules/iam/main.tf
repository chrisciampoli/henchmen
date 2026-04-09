# ---------------------------------------------------------------------------
# Service Accounts
# ---------------------------------------------------------------------------

resource "google_service_account" "mastermind" {
  project      = var.project_id
  account_id   = "sa-${var.environment}-mastermind"
  display_name = "Henchmen ${var.environment} Mastermind Service Account"
  description  = "Service account for the Mastermind orchestrator service"
}

resource "google_service_account" "dispatch" {
  project      = var.project_id
  account_id   = "sa-${var.environment}-dispatch"
  display_name = "Henchmen ${var.environment} Dispatch Service Account"
  description  = "Service account for the Dispatch ingress service"
}

resource "google_service_account" "operative" {
  project      = var.project_id
  account_id   = "sa-${var.environment}-operative"
  display_name = "Henchmen ${var.environment} Operative Service Account"
  description  = "Service account for Operative agent runner jobs"
}

resource "google_service_account" "arsenal" {
  project      = var.project_id
  account_id   = "sa-${var.environment}-arsenal"
  display_name = "Henchmen ${var.environment} Arsenal Service Account"
  description  = "Service account for the Arsenal MCP tool server"
}

resource "google_service_account" "forge" {
  project      = var.project_id
  account_id   = "sa-${var.environment}-forge"
  display_name = "Henchmen ${var.environment} Forge Service Account"
  description  = "Service account for the Forge CI pipeline service"
}

resource "google_service_account" "dossier" {
  project      = var.project_id
  account_id   = "sa-${var.environment}-dossier"
  display_name = "Henchmen ${var.environment} Dossier Service Account"
  description  = "Service account for the Dossier context builder service"
}

# ---------------------------------------------------------------------------
# IAM bindings: sa-mastermind
# ---------------------------------------------------------------------------

locals {
  mastermind_roles = [
    "roles/run.invoker",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
    "roles/datastore.user",
    "roles/aiplatform.user",
    "roles/run.developer",
    "roles/cloudtrace.agent",
  ]

  dispatch_roles = [
    "roles/run.invoker",
    "roles/pubsub.publisher",
    "roles/cloudtrace.agent",
  ]

  operative_roles = [
    "roles/pubsub.publisher",
    "roles/datastore.viewer",
    "roles/storage.objectViewer",
    "roles/aiplatform.user",
    "roles/cloudtrace.agent",
  ]

  arsenal_roles = [
    "roles/run.invoker",
  ]

  forge_roles = [
    "roles/cloudbuild.builds.editor",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
    "roles/datastore.user",
    "roles/storage.objectViewer",
    "roles/cloudtrace.agent",
  ]

  dossier_roles = [
    "roles/storage.objectAdmin",
    "roles/datastore.viewer",
  ]
}

resource "google_project_iam_member" "mastermind" {
  for_each = toset(local.mastermind_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.mastermind.email}"
}

resource "google_project_iam_member" "dispatch" {
  for_each = toset(local.dispatch_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.dispatch.email}"
}

resource "google_project_iam_member" "operative" {
  for_each = toset(local.operative_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.operative.email}"
}

resource "google_project_iam_member" "arsenal" {
  for_each = toset(local.arsenal_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.arsenal.email}"
}

resource "google_project_iam_member" "forge" {
  for_each = toset(local.forge_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.forge.email}"
}

resource "google_project_iam_member" "dossier" {
  for_each = toset(local.dossier_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.dossier.email}"
}
