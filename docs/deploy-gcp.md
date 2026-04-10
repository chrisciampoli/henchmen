# Deploying Henchmen on GCP

This guide walks a new self-hoster from a blank GCP account to a running
Henchmen stack in about 30 minutes. At the end you'll have:

- A new GCP project with the required APIs enabled
- A Terraform state bucket
- Cloud Run services for Dispatch, Mastermind, and Forge
- A Cloud Run Job template for the Operative (lair)
- Firestore, Pub/Sub, Artifact Registry, and Secret Manager provisioned
- Firestore security rules deployed via `google_firebaserules_release`
- A working `task-intake` pipeline you can exercise from the CLI

If you hit a wall, check `docs/troubleshooting.md` or open a discussion —
the README links to the GitHub Discussions board.

---

## Prerequisites

You'll need:

| Tool         | Version   | Install link                                       |
|--------------|-----------|----------------------------------------------------|
| `gcloud`     | latest    | https://cloud.google.com/sdk/docs/install          |
| `terraform`  | `>= 1.7`  | https://developer.hashicorp.com/terraform/downloads |
| `docker`     | `>= 24`   | https://docs.docker.com/get-docker/                |
| `git`        | any       | https://git-scm.com/                                |
| `python`     | `>= 3.12` | https://www.python.org/downloads/                   |

Plus a GCP billing account. If you don't have one yet, create it at
https://console.cloud.google.com/billing before you start — linking a
billing account is the only step that cannot be automated.

> Windows users: run the shell parts of this guide inside WSL or Git Bash.
> Everything else (gcloud, terraform, docker) has native Windows binaries.

---

## Step 1 — Clone the repo and install

```bash
git clone https://github.com/chrisciampoli/henchmen.git
cd henchmen
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[gcp,dev]"
```

Verify the install worked:

```bash
henchmen --help
```

You should see the top-level CLI help. If `henchmen: command not found`,
make sure your venv is active.

---

## Step 2 — Authenticate with gcloud

```bash
gcloud auth login
gcloud auth application-default login
```

The second command provisions
[Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials)
which Terraform and the Henchmen Python SDK both rely on.

---

## Step 3 — Bootstrap the project

Henchmen ships a one-shot bootstrap script that creates a new GCP
project, links it to your billing account, enables the minimum set of
APIs, and creates the Terraform state bucket:

```bash
export PROJECT_ID=my-henchmen-dev                 # pick any unused GCP project ID
export BILLING_ACCOUNT=01ABCD-23EFGH-45IJKL        # from `gcloud billing accounts list`

./scripts/bootstrap-gcp.sh --yes
```

The script is idempotent — rerunning it against an existing project is
safe. If the project already exists, it only creates the missing pieces.

After it finishes you should see:

```
✓ project ready:              my-henchmen-dev
✓ billing linked:             01ABCD-23EFGH-45IJKL
✓ terraform state bucket:     gs://henchmen-tfstate-dev
✓ next command:               cd terraform/environments/dev && terraform init
```

---

## Step 4 — Run Terraform

```bash
cd terraform/environments/dev

# Copy the example tfvars and fill in your GitHub + repo details
cp dev.auto.tfvars.example dev.auto.tfvars 2>/dev/null || true
$EDITOR dev.auto.tfvars

terraform init
terraform plan -out=tier1.tfplan
terraform apply tier1.tfplan
```

What this creates (abridged):

- Cloud Run services: `henchmen-dev-dispatch`, `henchmen-dev-mastermind`, `henchmen-dev-forge`
- Cloud Run Jobs: `henchmen-dev-lair-template` (cloned on every operative dispatch)
- Firestore in Native mode with the `henchmen-dev` database and indexes
- Firestore security rules (deployed via `google_firebaserules_release` — see `terraform/modules/data-stores/firestore.rules`)
- 10 Pub/Sub topics prefixed `henchmen-dev-*`
- A regional Artifact Registry repo `henchmen-dev`
- Secret Manager secrets for the tokens you'll populate in Step 6
- VPC connector and outbound egress rules

Expect the apply to take ~8 minutes. Artifact Registry and Firestore are
the slow ones.

---

## Step 5 — Build and push the container images

```bash
cd ../../..  # back to the repo root
REGION=us-central1

gcloud auth configure-docker ${REGION}-docker.pkg.dev

for svc in dispatch mastermind forge operative; do
  docker build -f containers/${svc}/Dockerfile \
    -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/${svc}:latest .
  docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/${svc}:latest
done

# Point the Cloud Run services at the images
for svc in dispatch mastermind forge; do
  gcloud run services update henchmen-dev-${svc} \
    --project=${PROJECT_ID} --region=${REGION} \
    --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/${svc}:latest
done

# Update the lair template to the fresh operative image
gcloud run jobs update henchmen-dev-lair-template \
  --project=${PROJECT_ID} --region=${REGION} \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/operative:latest
```

---

## Step 6 — Populate secrets

At minimum you need a GitHub token so the Operative can push branches
and open PRs. If you wire Slack, set those too.

```bash
# GitHub personal access token (classic) with `repo` scope
echo -n "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx" | \
  gcloud secrets versions add henchmen-dev-github-token --data-file=- --project=${PROJECT_ID}

# (optional) Slack bot + signing + app tokens
echo -n "xoxb-..." | gcloud secrets versions add henchmen-dev-slack-bot-token     --data-file=- --project=${PROJECT_ID}
echo -n "..."      | gcloud secrets versions add henchmen-dev-slack-signing-secret --data-file=- --project=${PROJECT_ID}
echo -n "xapp-..." | gcloud secrets versions add henchmen-dev-slack-app-token      --data-file=- --project=${PROJECT_ID}
```

> ⚠ Terraform re-renders Cloud Run env var blocks on every apply, which
> can wipe secret references back to their stub defaults. If that
> happens, re-run the `gcloud run services update` commands from Step 5.

---

## Step 7 — Smoke test

Dispatch a tiny CLI task against your Mastermind:

```bash
DISPATCH_URL=$(gcloud run services describe henchmen-dev-dispatch \
  --project=${PROJECT_ID} --region=${REGION} --format='value(status.url)')

curl -X POST "${DISPATCH_URL}/api/v1/tasks" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Fix the null check in src/auth/login.py",
    "description": "The login endpoint crashes when the password field is None.",
    "repo": "your-org/your-test-repo",
    "branch": "main",
    "priority": "normal",
    "created_by": "you@example.com"
  }'
```

Watch the logs stream in:

```bash
gcloud run services logs read henchmen-dev-mastermind \
  --project=${PROJECT_ID} --region=${REGION} --limit=50
```

If everything's wired correctly you should see the task get
normalized, a scheme selected, a lair job launched, and eventually a PR
opened on your test repo.

---

## Step 8 — Verify with `henchmen doctor`

```bash
henchmen doctor --env dev
```

This runs a self-check on the local SDK: Docker, gcloud, Python,
credentials, required Settings fields, git identity, and the remote
service health endpoints. Green across the board means you're done.

---

## Rollback

If something goes wrong during Terraform apply you can always:

```bash
cd terraform/environments/dev
terraform destroy          # tears down all provisioned GCP resources
```

See `docs/rollback-procedures.md` for the service-level rollback flow
(per-service image pinning and Cloud Run revision pinning).

---

## Where to go next

- `docs/architecture.md` — the seven-component Henchmen architecture
- `docs/schemes.md` — how schemes describe DAG workflows
- `docs/operations.md` — day-2 runbook for self-hosters
- `docs/troubleshooting.md` — common issues and fixes
- `evals/` — run the eval harness to measure BYO-LLM parity on your
  hardware and populate `evals/baseline.json` (see `.github/workflows/evals.yml`)
