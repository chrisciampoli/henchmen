# ---------------------------------------------------------------------------
# Firestore
#
# Access control is IAM-only. Firestore Security Rules are deliberately NOT
# used here: they are evaluated for Firebase client-SDK traffic, and every
# Henchmen component talks to Firestore through the server SDK, which
# authenticates with IAM and bypasses rules entirely. Per-writer isolation, if
# ever needed, means a separate database — not a ruleset.
# ---------------------------------------------------------------------------

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
# Object storage
#
# Bucket names are globally unique, so they are prefixed with the project ID.
# HENCHMEN_GCS_BUCKET_DOSSIER / _SNAPSHOTS are injected from the outputs below
# (see the cloud-run-services and cloud-run-lairs modules); without them the
# dossier upload is skipped and the snapshot cache raises.
# ---------------------------------------------------------------------------

resource "google_storage_bucket" "dossier" {
  project                     = var.project_id
  name                        = "${var.project_id}-henchmen-${var.environment}-dossier"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.environment != "prod"
  labels                      = var.labels

  lifecycle_rule {
    condition {
      age = var.artifact_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket" "snapshots" {
  project                     = var.project_id
  name                        = "${var.project_id}-henchmen-${var.environment}-snapshots"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.environment != "prod"
  labels                      = var.labels

  lifecycle_rule {
    condition {
      age = var.artifact_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

# Bucket-scoped storage access, in place of a project-level storage role.
# Mastermind builds and uploads dossiers; the operative reads its dossier and
# writes workspace snapshots.
resource "google_storage_bucket_iam_member" "dossier_mastermind" {
  bucket = google_storage_bucket.dossier.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.service_account_emails["mastermind"]}"
}

resource "google_storage_bucket_iam_member" "dossier_operative" {
  bucket = google_storage_bucket.dossier.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${var.service_account_emails["operative"]}"
}

resource "google_storage_bucket_iam_member" "dossier_forge" {
  bucket = google_storage_bucket.dossier.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${var.service_account_emails["forge"]}"
}

resource "google_storage_bucket_iam_member" "snapshots_mastermind" {
  bucket = google_storage_bucket.snapshots.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.service_account_emails["mastermind"]}"
}

resource "google_storage_bucket_iam_member" "snapshots_operative" {
  bucket = google_storage_bucket.snapshots.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.service_account_emails["operative"]}"
}
