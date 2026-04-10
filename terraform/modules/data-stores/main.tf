resource "google_firestore_database" "henchmen" {
  project     = var.project_id
  name        = "henchmen-${var.environment}"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  delete_protection_state = var.environment == "prod" ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"
}

# tasks: status ASC, created_at DESC
resource "google_firestore_index" "tasks_status_created_at" {
  project    = var.project_id
  database   = google_firestore_database.henchmen.name
  collection = "tasks"

  fields {
    field_path = "status"
    order      = "ASCENDING"
  }

  fields {
    field_path = "created_at"
    order      = "DESCENDING"
  }
}

# tasks: source ASC, status ASC
resource "google_firestore_index" "tasks_source_status" {
  project    = var.project_id
  database   = google_firestore_database.henchmen.name
  collection = "tasks"

  fields {
    field_path = "source"
    order      = "ASCENDING"
  }

  fields {
    field_path = "status"
    order      = "ASCENDING"
  }
}

# merge_queue: status ASC, created_at ASC
resource "google_firestore_index" "merge_queue_status_created_at" {
  project    = var.project_id
  database   = google_firestore_database.henchmen.name
  collection = "merge_queue"

  fields {
    field_path = "status"
    order      = "ASCENDING"
  }

  fields {
    field_path = "created_at"
    order      = "ASCENDING"
  }
}

# operative_reports: task_id ASC, completed_at DESC
resource "google_firestore_index" "operative_reports_task_id_completed_at" {
  project    = var.project_id
  database   = google_firestore_database.henchmen.name
  collection = "operative_reports"

  fields {
    field_path = "task_id"
    order      = "ASCENDING"
  }

  fields {
    field_path = "completed_at"
    order      = "DESCENDING"
  }
}

# ---------------------------------------------------------------------------
# Firestore security rules (least-privilege enforcement for the mastermind SA)
# ---------------------------------------------------------------------------
#
# Collection-level authorization cannot be expressed via project-level IAM
# bindings, so rules are deployed as a first-class terraform resource here.
# The firestore.rules file uses a single ${mastermind_sa_email_pattern}
# template variable so the email regex stays in sync with var.project_id.

locals {
  # Escape the dot in the project ID because it lands inside a regex match.
  _mastermind_sa_email_pattern = "sa-${var.environment}-mastermind@${replace(var.project_id, ".", "\\\\.")}\\\\.iam\\\\.gserviceaccount\\\\.com"
}

resource "google_firebaserules_ruleset" "firestore" {
  project = var.project_id

  source {
    files {
      name = "firestore.rules"
      content = templatefile("${path.module}/firestore.rules", {
        mastermind_sa_email_pattern = local._mastermind_sa_email_pattern
      })
    }
  }

  depends_on = [google_firestore_database.henchmen]
}

resource "google_firebaserules_release" "firestore" {
  project      = var.project_id
  name         = "cloud.firestore/${google_firestore_database.henchmen.name}"
  ruleset_name = "projects/${var.project_id}/rulesets/${google_firebaserules_ruleset.firestore.name}"

  depends_on = [google_firebaserules_ruleset.firestore]
}
