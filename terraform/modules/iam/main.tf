# ---------------------------------------------------------------------------
# IAM module — service accounts and their role bindings.
#
# Defense in depth: the conditions applied below (Vertex AI publisher scoping,
# Cloud Run service-name prefix scoping) are *secondary* controls. The primary
# enforcement boundary is still VPC Service Controls + the service perimeter
# configured around this project. If a role binding here is wrong but VPC-SC
# is correct, the blast radius is bounded by the perimeter. The conditions
# here exist to shrink the blast radius further and to make the least-
# privilege intent auditable from the terraform state alone.
#
# Firestore: collection-level ACLs cannot be expressed via google_project_iam
# resources. Per-collection authorization must be enforced by Firestore
# Security Rules (see terraform/modules/data-stores/firestore.rules) deployed
# via the Firebase CLI or a google_firebaserules_ruleset resource — out of
# scope for this module.
# ---------------------------------------------------------------------------

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
# Role sets (unconditioned, project-level bindings).
#
# Any role that needs a condition or resource-level narrowing is excluded
# from these lists and applied below as a dedicated resource.
# ---------------------------------------------------------------------------

locals {
  # NOTE: roles/datastore.user is intentionally NOT in this list. Firestore
  # does not support collection-level IAM via google_project_iam_member. The
  # least-privilege pattern for mastermind's Firestore access is to:
  #   1. Grant project-level datastore.user out of band (e.g. by gcloud or
  #      by a separate binding owned by the platform team), and
  #   2. Enforce per-collection authorization via Firestore Security Rules
  #      in terraform/modules/data-stores/firestore.rules, which must be
  #      deployed via the Firebase CLI or a google_firebaserules_ruleset
  #      resource — out of scope for this IAM module.
  #
  # roles/aiplatform.user and roles/run.developer are also excluded: they
  # are applied below with IAM conditions that scope them to Gemini publisher
  # models and to `henchmen-${environment}-` Cloud Run services respectively.
  mastermind_roles = [
    "roles/run.invoker",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
    "roles/cloudtrace.agent",
  ]

  dispatch_roles = [
    "roles/run.invoker",
    "roles/pubsub.publisher",
    "roles/cloudtrace.agent",
  ]

  # NOTE: roles/aiplatform.user is excluded here and applied below with a
  # condition restricting the operative to Gemini publisher models only.
  operative_roles = [
    "roles/pubsub.publisher",
    "roles/datastore.viewer",
    "roles/storage.objectViewer",
    "roles/cloudtrace.agent",
  ]

  arsenal_roles = [
    "roles/run.invoker",
  ]

  # NOTE: sa-forge currently holds no Cloud Run IAM role (roles/run.*), so
  # there is nothing to narrow via a service-name-prefix condition here. If
  # forge ever gains a Cloud Run role, add it as a dedicated conditioned
  # resource below matching the mastermind pattern.
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

# ---------------------------------------------------------------------------
# Conditioned / resource-scoped bindings (defense in depth).
# ---------------------------------------------------------------------------

# Operative: roles/aiplatform.user restricted to Gemini publisher models only.
# This is a HARD RULE in the Henchmen codebase — no Claude on Vertex AI — and
# is enforced here in addition to the application-level scheme configuration.
#
# The IAM condition uses the CAEL (Common Expression Language) expression
# evaluator. `resource.name` for Vertex AI predictions is of the form:
#   projects/<project>/locations/<region>/publishers/google/models/gemini-*
# Anything that does not start with that prefix (e.g. anthropic/claude-*,
# meta/llama-*) will be denied.
resource "google_project_iam_member" "operative_aiplatform_gemini_only" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.operative.email}"

  condition {
    title       = "gemini-publisher-models-only"
    description = "Restrict operative Vertex AI access to Gemini publisher models only. Denies Claude and all other non-Google publisher models."
    expression  = "resource.name.startsWith(\"projects/${var.project_id}/locations/${var.region}/publishers/google/models/gemini\")"
  }
}

# Mastermind: roles/run.developer restricted to `henchmen-${environment}-`
# Cloud Run services. This prevents a compromised Mastermind from creating or
# mutating unrelated Cloud Run services in the same project.
#
# Cloud Run service resource names are of the form:
#   projects/<project>/locations/<region>/services/<service-name>
# where <service-name> is e.g. `henchmen-dev-mastermind`.
resource "google_project_iam_member" "mastermind_run_developer_scoped" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.mastermind.email}"

  condition {
    title       = "henchmen-services-only"
    description = "Restrict mastermind Cloud Run admin access to henchmen-${var.environment}-* services only."
    expression  = "resource.name.startsWith(\"projects/${var.project_id}/locations/${var.region}/services/henchmen-${var.environment}-\")"
  }
}
