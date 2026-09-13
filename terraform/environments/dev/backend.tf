# Dev GCS backend.
#
# The bucket name is deliberately NOT hardcoded: GCS bucket names are globally
# unique, so a fixed `henchmen-tfstate-dev` can only ever belong to whoever
# created it first — everyone else gets 409 BucketNameUnavailable on create
# and a permission error on init. Supply it at init time instead:
#
#   gcloud storage buckets create gs://henchmen-tfstate-$PROJECT_ID-dev \
#     --location=us-central1 --uniform-bucket-level-access
#   terraform init -backend-config=bucket=henchmen-tfstate-$PROJECT_ID-dev
#
# Reconfiguring an existing deployment onto its current bucket is the same
# command with the old name.
terraform {
  backend "gcs" {
    prefix = "terraform/state"
  }
}
