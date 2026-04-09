locals {
  registry_base = "${var.region}-docker.pkg.dev/${var.project_id}/henchmen-${var.environment}"
}

# ---------------------------------------------------------------------------
# Cloud Build triggers require a GitHub repository connection to be set up
# first via the GCP Console (Settings > Repositories). Once connected,
# uncomment these triggers.
# ---------------------------------------------------------------------------

# resource "google_cloudbuild_trigger" "pr_ci" {
#   name     = "henchmen-${var.environment}-pr-ci"
#   location = var.region
#   project  = var.project_id
#
#   github {
#     owner = var.github_owner
#     name  = var.github_repo
#
#     pull_request {
#       branch = ".*"
#     }
#   }
#
#   build {
#     step {
#       name = "python:3.12"
#       args = ["pip", "install", "-e", ".[dev]"]
#     }
#     step {
#       name = "python:3.12"
#       args = ["python", "-m", "ruff", "check", "."]
#     }
#     step {
#       name = "python:3.12"
#       args = ["python", "-m", "pytest", "tests/", "-v"]
#     }
#
#     timeout = "600s"
#   }
# }

# resource "google_cloudbuild_trigger" "operative_image" {
#   name     = "henchmen-${var.environment}-operative-build"
#   location = var.region
#   project  = var.project_id
#
#   github {
#     owner = var.github_owner
#     name  = var.github_repo
#
#     push {
#       branch = "^main$"
#     }
#   }
#
#   build {
#     step {
#       name = "gcr.io/cloud-builders/docker"
#       args = [
#         "build",
#         "-t", "${local.registry_base}/operative:$SHORT_SHA",
#         "-t", "${local.registry_base}/operative:latest",
#         "-f", "containers/operative/Dockerfile",
#         ".",
#       ]
#     }
#     step {
#       name = "gcr.io/cloud-builders/docker"
#       args = ["push", "${local.registry_base}/operative:$SHORT_SHA"]
#     }
#     step {
#       name = "gcr.io/cloud-builders/docker"
#       args = ["push", "${local.registry_base}/operative:latest"]
#     }
#
#     timeout = "600s"
#   }
# }
