locals {
  registry_base = "${var.region}-docker.pkg.dev/${var.project_id}/henchmen-${var.environment}"
}

# ---------------------------------------------------------------------------
# Cloud Build triggers.
#
# These resources are gated on var.enable_cloud_build (default: false)
# because they require a GitHub repository connection to be created
# manually via the GCP Console (Settings > Repositories > Connect
# Repository) before terraform can reference them. See the variable
# description in variables.tf for the opt-in workflow.
#
# When disabled, the module provisions zero Cloud Build resources and
# outputs return `null` (see outputs.tf) rather than empty strings.
# ---------------------------------------------------------------------------

resource "google_cloudbuild_trigger" "pr_ci" {
  count = var.enable_cloud_build ? 1 : 0

  name     = "henchmen-${var.environment}-pr-ci"
  location = var.region
  project  = var.project_id

  github {
    owner = var.github_owner
    name  = var.github_repo

    pull_request {
      branch = ".*"
    }
  }

  build {
    step {
      name = "python:3.12"
      args = ["pip", "install", "-e", ".[dev]"]
    }
    step {
      name = "python:3.12"
      args = ["python", "-m", "ruff", "check", "."]
    }
    step {
      name = "python:3.12"
      args = ["python", "-m", "pytest", "tests/", "-v"]
    }

    timeout = "600s"
  }
}

resource "google_cloudbuild_trigger" "operative_image" {
  count = var.enable_cloud_build ? 1 : 0

  name     = "henchmen-${var.environment}-operative-build"
  location = var.region
  project  = var.project_id

  github {
    owner = var.github_owner
    name  = var.github_repo

    push {
      branch = "^main$"
    }
  }

  build {
    step {
      name = "gcr.io/cloud-builders/docker"
      args = [
        "build",
        "-t", "${local.registry_base}/operative:$SHORT_SHA",
        "-t", "${local.registry_base}/operative:latest",
        "-f", "containers/operative/Dockerfile",
        ".",
      ]
    }
    step {
      name = "gcr.io/cloud-builders/docker"
      args = ["push", "${local.registry_base}/operative:$SHORT_SHA"]
    }
    step {
      name = "gcr.io/cloud-builders/docker"
      args = ["push", "${local.registry_base}/operative:latest"]
    }

    timeout = "600s"
  }
}
