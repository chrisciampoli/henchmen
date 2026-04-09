output "database_name" {
  description = "The name of the Firestore database"
  value       = google_firestore_database.henchmen.name
}

output "database_id" {
  description = "The ID of the Firestore database"
  value       = google_firestore_database.henchmen.id
}
