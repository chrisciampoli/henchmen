# Staging GCS backend. The bucket must exist before `terraform init` and
# cannot be managed by terraform itself (chicken-and-egg). Create it once:
#   gcloud storage buckets create gs://henchmen-tfstate-staging \
#     --location=us-central1 --uniform-bucket-level-access
#
# Then run:
#   terraform init -backend-config=bucket=henchmen-tfstate-staging
terraform {
  backend "gcs" {
    bucket = "henchmen-tfstate-staging"
    prefix = "terraform/state"
  }
}
