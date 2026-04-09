# PREREQUISITE: The GCS bucket referenced below must be created manually before
# running `terraform init`. It cannot be managed by Terraform itself because the
# backend must exist before any state can be stored.
#
# Choose a globally-unique bucket name and create it with:
#   gcloud storage buckets create gs://<YOUR_BUCKET_NAME> \
#     --location=us-central1 --uniform-bucket-level-access
#
# Then update the bucket value below and run: terraform init
terraform {
  backend "gcs" {
    # bucket = "<YOUR_GCS_BUCKET_FOR_TF_STATE>"
    bucket = "henchmen-tfstate-staging"
    prefix = "terraform/state"
  }
}
