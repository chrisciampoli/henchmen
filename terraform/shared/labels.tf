locals {
  labels = {
    project     = var.project_name
    environment = var.environment
    managed_by  = "terraform"
  }
}
