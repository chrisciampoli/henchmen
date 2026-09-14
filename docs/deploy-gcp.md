# Deploying Henchmen on GCP

This guide walks a new self-hoster from a blank GCP account to a running
Henchmen stack in about 30 minutes. At the end you'll have:

- A new GCP project with the required APIs enabled
- A Terraform state bucket
- Cloud Run services for Dispatch, Mastermind, and Forge
- A reference Cloud Run Job for the Operative (the lair template)
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
project, links it to your billing account, and enables the Service Usage
API so Terraform can enable everything else:

```bash
export PROJECT_ID=my-henchmen-dev                 # pick any unused GCP project ID
export BILLING_ACCOUNT=01ABCD-23EFGH-45IJKL        # from `gcloud billing accounts list`
export REGION=us-central1

./scripts/bootstrap-gcp.sh --yes
```

The script is idempotent — rerunning it against an existing project is
safe. If the project already exists, it only creates the missing pieces.
Each step logs a `==> ...` line, and the run ends with `Bootstrap complete.`
followed by a `Next steps:` list.

---

## Step 4 — Run Terraform

```bash
cd terraform/environments/dev

# Your identity values: project_id, github_owner, github_default_repo.
# terraform.tfvars is git-ignored and auto-loaded. Do not copy the example over
# dev.auto.tfvars — that file is committed and carries the dev sizing.
cp dev.auto.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

# State bucket names are globally unique, so the name includes the project ID.
gcloud storage buckets create gs://henchmen-tfstate-${PROJECT_ID}-dev \
  --project=${PROJECT_ID} --location=${REGION} --uniform-bucket-level-access
gcloud storage buckets update gs://henchmen-tfstate-${PROJECT_ID}-dev --versioning
terraform init -backend-config=bucket=henchmen-tfstate-${PROJECT_ID}-dev

terraform plan -out=tier1.tfplan
terraform apply tier1.tfplan
```

What this creates (abridged):

- Cloud Run services: `henchmen-dev-dispatch`, `henchmen-dev-mastermind`, `henchmen-dev-forge`
- Cloud Run Job: `henchmen-dev-lair-template` — a reference copy of the operative job spec for review and `gcloud run jobs execute` smoke tests. Mastermind never clones it; see [How operatives are launched](#how-operatives-are-launched).
- Firestore in Native mode with the `henchmen-dev` database and indexes
- Firestore security rules (deployed via `google_firebaserules_release` — see `terraform/modules/data-stores/firestore.rules`)
- 7 Pub/Sub topics prefixed `henchmen-dev-*`: `task-intake`, `operative-complete`, `forge-request`, `forge-result`, `ci-failure`, `embed-request`, `dead-letter`
- A regional Artifact Registry repo `henchmen-dev`
- Secret Manager secrets, seeded with placeholder versions, for the tokens you'll populate in Step 6
- A VPC and Serverless VPC Access connector for private-range traffic (public egress does not traverse it, and there are no egress firewall rules)

The first apply deploys a public placeholder image to every service, because
no Henchmen image exists yet. Expect it to take ~8 minutes. Artifact Registry
and Firestore are the slow ones.

---

## Step 5 — Build and push the container images

You can build the images yourself, or start from the prebuilt release images
(see [Prebuilt images](#prebuilt-images)).

```bash
cd ../../..  # back to the repo root

gcloud auth configure-docker ${REGION}-docker.pkg.dev

for svc in dispatch mastermind forge operative; do
  docker build -f containers/${svc}/Dockerfile \
    -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/${svc}:latest .
  docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/${svc}:latest
done
```

Then point Terraform at the images. Terraform owns each service's image, so
set the tag in `terraform.tfvars` rather than with `gcloud run services update`
(a hand-set image is reverted by the next apply):

```bash
cd terraform/environments/dev
echo 'container_image_tag = "latest"' >> terraform.tfvars
terraform apply
```

The same tag is injected into Mastermind as `HENCHMEN_LAIR_OPERATIVE_IMAGE_TAG`,
which selects the operative image every lair runs.

### Prebuilt images

Every tagged release (`v*`) builds all four containers and pushes them to the
GitHub Container Registry (`.github/workflows/release.yml`):

| Image | Tags |
|-------|------|
| `ghcr.io/chrisciampoli/henchmen/dispatch` | `X.Y.Z`, `latest` |
| `ghcr.io/chrisciampoli/henchmen/mastermind` | `X.Y.Z`, `latest` |
| `ghcr.io/chrisciampoli/henchmen/forge` | `X.Y.Z`, `latest` |
| `ghcr.io/chrisciampoli/henchmen/operative` | `X.Y.Z`, `latest` |

`X.Y.Z` is the release tag without its leading `v` (tag `v0.2.1` publishes
`:0.2.1`). Pin a version in production; `latest` moves with every release.

```bash
docker pull ghcr.io/chrisciampoli/henchmen/mastermind:0.2.1
```

Cloud Run cannot pull from GHCR directly — it only deploys images from
Artifact Registry (or Docker Hub). Either copy the images into your
`henchmen-dev` repository:

```bash
VERSION=0.2.1
for svc in dispatch mastermind forge operative; do
  docker pull ghcr.io/chrisciampoli/henchmen/${svc}:${VERSION}
  docker tag  ghcr.io/chrisciampoli/henchmen/${svc}:${VERSION} \
    ${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/${svc}:${VERSION}
  docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/${svc}:${VERSION}
done
```

and set `container_image_tag = "0.2.1"`. An Artifact Registry remote
repository that proxies `ghcr.io` works for the three services (point the
`container_images` map at it), but the operative must still be copied: Mastermind
always launches lairs from `…/henchmen-dev/operative:<tag>`.

If `docker pull` is denied, the packages are not public on your fork — run
`echo $GITHUB_PAT | docker login ghcr.io -u <user> --password-stdin` with a
token that has `read:packages`.

### How operatives are launched

For every agentic scheme node, Mastermind calls the Cloud Run Jobs API to
create a new job named `lair-<task>-<node>-<suffix>`, runs it once, and waits
for the operative's report. The job is built from scratch each time
(`src/henchmen/providers/gcp/cloud_run.py`) using:

- image `${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/operative:${HENCHMEN_LAIR_OPERATIVE_IMAGE_TAG}`
- CPU, memory and timeout from `HENCHMEN_LAIR_DEFAULT_CPU` / `_MEMORY` and the node's `timeout_seconds`
- service account `sa-dev-operative` (override with `HENCHMEN_LAIR_SERVICE_ACCOUNT`)
- the `henchmen-dev-github-token` secret mounted as `GITHUB_TOKEN`

`henchmen-dev-lair-template` is rendered from the same values so the spec is
reviewable, but updating it changes nothing about what operatives run.

---

## Step 6 — Populate secrets

At minimum you need a GitHub token so the Operative can push branches
and open PRs. If you wire Slack or Jira, set those too.

```bash
# GitHub personal access token (classic) with `repo` scope
echo -n "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx" | \
  gcloud secrets versions add henchmen-dev-github-token --data-file=- --project=${PROJECT_ID}

# Bearer token for the /metrics endpoints
openssl rand -hex 32 | tr -d '\n' | \
  gcloud secrets versions add henchmen-dev-metrics-auth-token --data-file=- --project=${PROJECT_ID}

# Bearer token for Dispatch's POST /api/v1/tasks. Keep a copy: you send it in
# Step 7. Cloud Run mounts it on Dispatch as DISPATCH_API_TOKEN.
export HENCHMEN_DISPATCH_API_TOKEN=$(openssl rand -hex 32)
echo -n "${HENCHMEN_DISPATCH_API_TOKEN}" | \
  gcloud secrets versions add henchmen-dev-dispatch-api-token --data-file=- --project=${PROJECT_ID}

# (optional) Slack bot + signing + app tokens
echo -n "xoxb-..." | gcloud secrets versions add henchmen-dev-slack-bot-token      --data-file=- --project=${PROJECT_ID}
echo -n "..."      | gcloud secrets versions add henchmen-dev-slack-signing-secret --data-file=- --project=${PROJECT_ID}
echo -n "xapp-..." | gcloud secrets versions add henchmen-dev-slack-app-token      --data-file=- --project=${PROJECT_ID}

# (optional) Jira API token
echo -n "..."      | gcloud secrets versions add henchmen-dev-jira-api-token       --data-file=- --project=${PROJECT_ID}
```

Then set `seed_secret_placeholders = false` in `terraform.tfvars` so a later
apply cannot add a placeholder version that shadows your real values, and
apply once more.

Cloud Run resolves a `latest` secret when an instance starts, so instances that
were already running keep the placeholder. Until Dispatch restarts with the
real `dispatch-api-token`, `POST /api/v1/tasks` returns 401 in staging and prod
(Dispatch treats the seeded placeholder as "no token"); in dev it stays open and
logs a warning. Force new revisions after adding the versions (the throwaway
variable only exists to create a revision; the next `terraform apply` removes
it again, which is harmless):

```bash
for svc in dispatch mastermind forge; do
  gcloud run services update henchmen-dev-${svc} --project=${PROJECT_ID} --region=${REGION} \
    --update-env-vars=SECRETS_ROTATED_AT=$(date +%s)
done
```

> Terraform owns every environment variable and secret mount on the Cloud Run
> services. It does not strip the secrets it manages, but anything added by
> hand with `gcloud run services update --set-env-vars` / `--set-secrets` is
> removed by the next apply. Add such values to the `cloud-run-services`
> module instead.

---

## Step 7 — Smoke test

Dispatch a tiny CLI task against your Mastermind. Two tokens are involved:

- **The Dispatch API token** (`HENCHMEN_DISPATCH_API_TOKEN`, from Step 6) goes in
  `Authorization: Bearer ...`. Dispatch checks it on `POST /api/v1/tasks`.
- **A Google identity token** is also needed while `dispatch_public_ingress` is
  `false` (the default), because Cloud Run IAM then rejects unauthenticated
  callers before the request reaches Dispatch. Cloud Run IAM normally reads
  `Authorization` too, so send the identity token in
  `X-Serverless-Authorization` instead and leave `Authorization` for Dispatch.
  With `dispatch_public_ingress = true` drop that header.

```bash
DISPATCH_URL=$(gcloud run services describe henchmen-dev-dispatch \
  --project=${PROJECT_ID} --region=${REGION} --format='value(status.url)')

curl -X POST "${DISPATCH_URL}/api/v1/tasks" \
  -H "X-Serverless-Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Authorization: Bearer ${HENCHMEN_DISPATCH_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Fix the null check in src/auth/login.py",
    "description": "The login endpoint crashes when the password field is None.",
    "repo": "your-org/your-test-repo",
    "branch": "main",
    "priority": "normal",
    "task_type": "bugfix",
    "created_by": "you@example.com"
  }'
```

A 401 with `Dispatch API token is not configured` means Dispatch is still on
the placeholder secret (see the restart note in Step 6); `Missing or invalid
bearer token` means the `Authorization` value does not match the secret. A 403
from Cloud Run means the identity token is missing or your account lacks
`roles/run.invoker` on Dispatch.

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
henchmen doctor
```

`doctor` builds the same `Settings` the services use, so it reads your
`.env.local`, then checks Python, Docker, git identity, the operative image,
and every credential you configured — including whether the GitHub token can
push to your default repo. It also prints the model each tier resolves to.
Add `--offline` to skip the network probes. Green across the board means the
local side is ready; the Cloud Run services are checked by their own
`/health` endpoints.

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
