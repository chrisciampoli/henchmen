locals {
  apis = [
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "pubsub.googleapis.com",
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudscheduler.googleapis.com",
    "aiplatform.googleapis.com",
    "compute.googleapis.com",
    "vpcaccess.googleapis.com",
    "iam.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.apis)

  project                    = var.project_id
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}
