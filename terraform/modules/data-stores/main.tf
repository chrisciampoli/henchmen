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
