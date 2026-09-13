# ---------------------------------------------------------------------------
# IAM module — service accounts and their role bindings.
#
# Defense in depth: the conditions applied below (Vertex AI publisher scoping)
# are *secondary* controls. The primary enforcement boundary is still VPC
# Service Controls + the service perimeter configured around this project. If
# a role binding here is wrong but VPC-SC is correct, the blast radius is
# bounded by the perimeter. The conditions here exist to shrink the blast
# radius further and to make the least-privilege intent auditable from the
# terraform state alone.
#
# Conditions are deliberately written so that they cannot *silently* deny a
# call the runtime actually makes: a condition that matches no real resource
# name is indistinguishable from having no binding at all. See the comment on
# the Vertex AI binding below.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Service Accounts
#
# One per workload identity. Arsenal has no service account because it runs
# in-process inside the Operative container, and Dossier has none because it
# is a library that runs inside Mastermind.
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

resource "google_service_account" "forge" {
  project      = var.project_id
  account_id   = "sa-${var.environment}-forge"
  display_name = "Henchmen ${var.environment} Forge Service Account"
  description  = "Service account for the Forge CI pipeline service"
}

# Caller identity for Pub/Sub push deliveries. Kept separate from the workload
# identities so that a leaked Mastermind token cannot be replayed against
# Dispatch/Forge as a legitimate push delivery, and so that
# HENCHMEN_PUBSUB_OIDC_ALLOWED_EMAILS can name exactly one publisher.
resource "google_service_account" "pubsub_push" {
  project      = var.project_id
  account_id   = "sa-${var.environment}-pubsub-push"
  display_name = "Henchmen ${var.environment} Pub/Sub Push Service Account"
  description  = "OIDC identity Pub/Sub uses to authenticate push deliveries to Cloud Run"
}

# Caller identity for Cloud Scheduler jobs (watchdog, DLQ check, cleanup,
# merge queue). Only ever needs run.invoker on the two services it calls.
resource "google_service_account" "scheduler" {
  project      = var.project_id
  account_id   = "sa-${var.environment}-scheduler"
  display_name = "Henchmen ${var.environment} Cloud Scheduler Service Account"
  description  = "OIDC identity Cloud Scheduler uses to invoke Mastermind and Forge"
}

# ---------------------------------------------------------------------------
# Role sets (unconditioned, project-level bindings).
#
# Any role that needs a condition or resource-level narrowing is excluded
# from these lists and applied below as a dedicated resource. Bucket-scoped
# storage roles live in the data-stores module next to the buckets.
#
# roles/run.invoker is deliberately NOT granted at project level to any
# workload identity: nothing in Henchmen calls another Cloud Run service
# directly (all service-to-service traffic goes through Pub/Sub). The two
# caller identities above get service-level run.invoker in the
# cloud-run-services module instead.
# ---------------------------------------------------------------------------

locals {
  # roles/run.developer is required *unconditioned* here: Mastermind creates
  # and runs Cloud Run **Jobs** (`lair-<task>-<node>`) whose resource names are
  # `projects/<p>/locations/<r>` (jobs.create) and
  # `projects/<p>/locations/<r>/jobs/lair-...` (jobs.run). A condition scoped
  # to `/services/henchmen-<env>-` never matches any of them and therefore
  # denies every lair provisioning.
  mastermind_roles = [
    "roles/run.developer",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
    "roles/datastore.user",
    "roles/cloudtrace.agent",
  ]

  dispatch_roles = [
    "roles/pubsub.publisher",
    "roles/cloudtrace.agent",
  ]

  # roles/datastore.user (not .viewer): the operative writes `last_heartbeat`,
  # partial reports and cost accumulation to Firestore.
  operative_roles = [
    "roles/pubsub.publisher",
    "roles/datastore.user",
    "roles/cloudtrace.agent",
  ]

  forge_roles = [
    "roles/cloudbuild.builds.editor",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
    "roles/datastore.user",
    "roles/cloudtrace.agent",
  ]

  # Vertex AI callers. Mastermind queries the RAG corpus while building
  # dossiers; the operative runs the coding models.
  aiplatform_members = {
    mastermind = google_service_account.mastermind.email
    operative  = google_service_account.operative.email
  }
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

resource "google_project_iam_member" "forge" {
  for_each = toset(local.forge_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.forge.email}"
}

# ---------------------------------------------------------------------------
# Conditioned / resource-scoped bindings (defense in depth).
# ---------------------------------------------------------------------------

# roles/aiplatform.user, denied for every non-Google publisher model. This is
# the IAM half of the HARD RULE "no Claude models on Vertex AI"; the other
# half is the model tiering in Settings / scheme nodes.
#
# The condition is written as "the publisher segment is absent or is google"
# rather than as a `startsWith(".../publishers/google/models/gemini")`
# allow-list, because the latter also denies every Vertex AI resource that is
# not a publisher model — RAG corpora (`/ragCorpora/`), context caches
# (`/cachedContents/`), experiments and evaluation — which Henchmen uses.
# `extract()` returns "" when the pattern does not match, so non-publisher
# resources fall through the first clause and remain allowed.
resource "google_project_iam_member" "aiplatform_google_publishers_only" {
  for_each = local.aiplatform_members

  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${each.value}"

  condition {
    title       = "google-publisher-models-only"
    description = "Deny Vertex AI publisher models from publishers other than Google (no Claude on Vertex AI). Non-publisher Vertex AI resources are unaffected."
    expression  = "resource.name.extract(\"/publishers/{publisher}/\") == \"\" || resource.name.extract(\"/publishers/{publisher}/\") == \"google\""
  }
}

# Mastermind creates Cloud Run Jobs that *run as* the operative service
# account. Cloud Run requires the caller to hold iam.serviceAccounts.actAs on
# that service account; roles/run.developer does not include it, so without
# this binding every create_job fails with
# "Permission 'iam.serviceaccounts.actAs' denied on service account ...".
resource "google_service_account_iam_member" "mastermind_actas_operative" {
  service_account_id = google_service_account.operative.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.mastermind.email}"
}
