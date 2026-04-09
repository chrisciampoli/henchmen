#!/usr/bin/env bash
# scripts/bootstrap-gcp.sh — One-shot GCP project bootstrap for Henchmen self-hosters.
# Usage:
#   PROJECT_ID=my-henchmen-dev BILLING_ACCOUNT=01ABCD-... ./scripts/bootstrap-gcp.sh
# or with a flag to skip confirmations:
#   PROJECT_ID=... BILLING_ACCOUNT=... ./scripts/bootstrap-gcp.sh --yes
#
# -----------------------------------------------------------------------------
# What this script does
# -----------------------------------------------------------------------------
# Brings a brand-new GCP project up to the point where `terraform apply` in
# `terraform/environments/<env>/` will succeed against it. Specifically it:
#
#   1. Verifies that `gcloud`, `terraform`, and `gsutil` are installed.
#   2. Verifies that you are authenticated with gcloud.
#   3. Creates the GCP project (idempotent — skips if it already exists).
#   4. Links the project to a billing account.
#   5. Enables `serviceusage.googleapis.com` so Terraform can enable the rest.
#   6. Creates a regional GCS bucket for Terraform state, with versioning on.
#   7. Prints the exact next commands you should run.
#
# -----------------------------------------------------------------------------
# Prerequisites
# -----------------------------------------------------------------------------
#   - gcloud CLI           https://cloud.google.com/sdk/docs/install
#   - terraform >= 1.5     https://developer.hashicorp.com/terraform/downloads
#   - gsutil               ships with the gcloud CLI
#   - An active gcloud login: `gcloud auth login`
#   - An existing GCP billing account ID (e.g. `01ABCD-23EFGH-45IJKL`)
#
# -----------------------------------------------------------------------------
# Required env vars / CLI flags
# -----------------------------------------------------------------------------
#   PROJECT_ID         (required)  GCP project to create or reuse
#   BILLING_ACCOUNT    (required)  Billing account ID to link
#   GITHUB_OWNER       (optional)  GitHub owner for app setup later
#   REGION             (default: us-central1)
#   ENVIRONMENT        (default: dev)
#
# Flags:
#   --yes              Skip interactive confirmations
#   -h, --help         Print this header and exit
#
# -----------------------------------------------------------------------------
# Running on Windows
# -----------------------------------------------------------------------------
# This script is POSIX bash. On Windows, run it from WSL or Git Bash.
# After cloning the repo, make it executable once:
#
#   chmod +x scripts/bootstrap-gcp.sh
#
# -----------------------------------------------------------------------------

set -euo pipefail

# -----------------------------------------------------------------------------
# Defaults and argument parsing
# -----------------------------------------------------------------------------

REGION="${REGION:-us-central1}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
GITHUB_OWNER="${GITHUB_OWNER:-}"
AUTO_YES=0

print_help() {
  sed -n '2,55p' "$0"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes)
      AUTO_YES=1
      shift
      ;;
    --project-id)
      PROJECT_ID="$2"
      shift 2
      ;;
    --billing-account)
      BILLING_ACCOUNT="$2"
      shift 2
      ;;
    --region)
      REGION="$2"
      shift 2
      ;;
    --environment)
      ENVIRONMENT="$2"
      shift 2
      ;;
    --github-owner)
      GITHUB_OWNER="$2"
      shift 2
      ;;
    -h|--help)
      print_help
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Run with --help for usage." >&2
      exit 2
      ;;
  esac
done

# -----------------------------------------------------------------------------
# Required input validation
# -----------------------------------------------------------------------------

: "${PROJECT_ID:?PROJECT_ID is required (set env var or pass --project-id)}"
: "${BILLING_ACCOUNT:?BILLING_ACCOUNT is required (set env var or pass --billing-account)}"

STATE_BUCKET="henchmen-tfstate-${ENVIRONMENT}"

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

log() {
  printf '==> %s\n' "$*"
}

warn() {
  printf 'WARN: %s\n' "$*" >&2
}

err() {
  printf 'ERROR: %s\n' "$*" >&2
}

confirm() {
  # $1 = prompt text
  if [ "$AUTO_YES" -eq 1 ]; then
    return 0
  fi
  printf '%s [y/N] ' "$1"
  read -r reply
  case "$reply" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

require_cmd() {
  # $1 = command name, $2 = install hint
  if ! command -v "$1" >/dev/null 2>&1; then
    err "$1 is not installed or not on PATH."
    err "Install it: $2"
    exit 1
  fi
}

# -----------------------------------------------------------------------------
# Step 1: CLI prerequisites
# -----------------------------------------------------------------------------

log "Checking prerequisites..."
require_cmd gcloud    "https://cloud.google.com/sdk/docs/install"
require_cmd terraform "https://developer.hashicorp.com/terraform/downloads"
require_cmd gsutil    "ships with gcloud CLI; try: gcloud components install gsutil"

# -----------------------------------------------------------------------------
# Step 2: gcloud authentication
# -----------------------------------------------------------------------------

log "Checking gcloud authentication..."
ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)"
if [ -z "$ACTIVE_ACCOUNT" ]; then
  err "No active gcloud account. Run: gcloud auth login"
  exit 1
fi
log "Active gcloud account: $ACTIVE_ACCOUNT"

# -----------------------------------------------------------------------------
# Summary + confirmation
# -----------------------------------------------------------------------------

cat <<EOF

Henchmen GCP bootstrap
----------------------
  PROJECT_ID      : $PROJECT_ID
  BILLING_ACCOUNT : $BILLING_ACCOUNT
  REGION          : $REGION
  ENVIRONMENT     : $ENVIRONMENT
  GITHUB_OWNER    : ${GITHUB_OWNER:-<not set>}
  STATE_BUCKET    : gs://$STATE_BUCKET
  ACTIVE_ACCOUNT  : $ACTIVE_ACCOUNT

EOF

if ! confirm "Proceed with bootstrap?"; then
  log "Aborted by user."
  exit 0
fi

# -----------------------------------------------------------------------------
# Step 3: Create the project (idempotent)
# -----------------------------------------------------------------------------

log "Checking whether project $PROJECT_ID exists..."
if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  log "Project $PROJECT_ID already exists, skipping create."
else
  if confirm "Create GCP project $PROJECT_ID?"; then
    log "Creating project $PROJECT_ID..."
    gcloud projects create "$PROJECT_ID" --name="Henchmen $ENVIRONMENT"
  else
    err "Project $PROJECT_ID does not exist and creation was declined."
    exit 1
  fi
fi

# -----------------------------------------------------------------------------
# Step 4: Link billing
# -----------------------------------------------------------------------------

log "Checking billing linkage for $PROJECT_ID..."
CURRENT_BILLING="$(gcloud beta billing projects describe "$PROJECT_ID" \
  --format='value(billingAccountName)' 2>/dev/null || true)"

if [ -n "$CURRENT_BILLING" ] && echo "$CURRENT_BILLING" | grep -q "$BILLING_ACCOUNT"; then
  log "Billing already linked to $BILLING_ACCOUNT."
else
  if confirm "Link project $PROJECT_ID to billing account $BILLING_ACCOUNT?"; then
    log "Linking billing..."
    gcloud beta billing projects link "$PROJECT_ID" \
      --billing-account="$BILLING_ACCOUNT"
  else
    err "Billing link declined; Terraform will fail without billing."
    exit 1
  fi
fi

# -----------------------------------------------------------------------------
# Step 5: Enable serviceusage.googleapis.com
# -----------------------------------------------------------------------------

log "Enabling serviceusage.googleapis.com (required before Terraform can enable other APIs)..."
gcloud services enable serviceusage.googleapis.com \
  --project="$PROJECT_ID"

# -----------------------------------------------------------------------------
# Step 6: Create the Terraform state bucket
# -----------------------------------------------------------------------------

log "Checking for Terraform state bucket gs://$STATE_BUCKET..."
if gsutil ls -b "gs://$STATE_BUCKET" >/dev/null 2>&1; then
  log "State bucket gs://$STATE_BUCKET already exists, skipping create."
else
  if confirm "Create state bucket gs://$STATE_BUCKET in $REGION?"; then
    log "Creating gs://$STATE_BUCKET ..."
    gsutil mb -p "$PROJECT_ID" -l "$REGION" "gs://$STATE_BUCKET"
  else
    err "State bucket creation declined; Terraform backend cannot initialise."
    exit 1
  fi
fi

# -----------------------------------------------------------------------------
# Step 7: Enable versioning on the state bucket
# -----------------------------------------------------------------------------

log "Enabling versioning on gs://$STATE_BUCKET ..."
gsutil versioning set on "gs://$STATE_BUCKET"

# -----------------------------------------------------------------------------
# Next steps
# -----------------------------------------------------------------------------

cat <<EOF

Bootstrap complete.

Next steps:
  1. Review ${ENVIRONMENT}.auto.tfvars in terraform/environments/${ENVIRONMENT}/
     (contains the env-specific sizing/scheduler/allowlist values).
     The shared module composition lives in terraform/environments/root/.

  2. Initialise and apply Terraform:

       cd terraform/environments/${ENVIRONMENT}
       terraform init -backend-config=bucket=${STATE_BUCKET}
       terraform apply

  3. After the first apply, populate Secret Manager secrets
     (GitHub token, Slack tokens, Jira token) via:

       gcloud secrets versions add henchmen-${ENVIRONMENT}-github-token \\
         --project=${PROJECT_ID} --data-file=-

  4. Build and push container images, then redeploy Cloud Run services
     (see README.md "Container Build & Deploy" section).

EOF
