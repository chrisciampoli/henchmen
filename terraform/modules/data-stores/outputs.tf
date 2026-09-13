output "database_name" {
  description = "The name of the Firestore database"
  value       = google_firestore_database.henchmen.name
}

output "database_id" {
  description = "The ID of the Firestore database"
  value       = google_firestore_database.henchmen.id
}

output "dossier_bucket_name" {
  description = "The name of the dossier artifact bucket (HENCHMEN_GCS_BUCKET_DOSSIER)"
  value       = google_storage_bucket.dossier.name
}

output "snapshots_bucket_name" {
  description = "The name of the operative snapshot bucket (HENCHMEN_GCS_BUCKET_SNAPSHOTS)"
  value       = google_storage_bucket.snapshots.name
}
