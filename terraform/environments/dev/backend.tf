# Dev GCS backend. The bucket must exist before `terraform init` and cannot be
# managed by terraform itself (chicken-and-egg). Create it once with:
#   gcloud storage buckets create gs://henchmen-tfstate-dev \
#     --location=us-central1 --uniform-bucket-level-access
#
# Then run:
#   terraform init -backend-config=bucket=henchmen-tfstate-dev
terraform {
  backend "gcs" {
    bucket = "henchmen-tfstate-dev"
    prefix = "terraform/state"
  }
}
