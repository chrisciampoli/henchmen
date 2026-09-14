# Operations Guide

This is the day-2 runbook for a self-hosted Henchmen stack on GCP. For
first-time provisioning, start with [`deploy-gcp.md`](deploy-gcp.md) —
that doc walks you from a blank GCP account to a running stack in
~30 minutes. This guide picks up once your stack is live and covers:

- building + pushing new container images
- updating a single Cloud Run service
- populating secrets after rotation
- diagnosing common runtime problems
- reading task execution state from Firestore

If you're running Henchmen in local mode (`henchmen serve`, or
`docker compose up`, which runs the same single process), most of this guide
still applies — GCP names map to local equivalents as follows:

| GCP resource              | Local equivalent                                                   |
|---------------------------|--------------------------------------------------------------------|
| Cloud Run services        | one `henchmen serve` process, services mounted at `/dispatch`, `/mastermind`, `/forge` on port 8000 |
| Cloud Run Jobs (Lairs)    | Docker containers from `henchmen-operative:local`                  |
| Firestore                 | SQLite at `~/.henchmen/henchmen_<env>.db` (`HENCHMEN_LOCAL_SQLITE_PATH`) |
| Cloud Storage             | files under `~/.henchmen/storage` (`HENCHMEN_LOCAL_STORAGE_DIR`)   |
| Pub/Sub                   | in-memory broker that HTTP-forwards to the mounted services        |
| Secret Manager            | `.env.local`                                                       |
| Cloud Scheduler           | a local cron hitting `http://localhost:8000/mastermind/api/v1/watchdog` |
| Cloud Logging             | stdout of `henchmen serve` (`docker logs henchmen` under compose)   |

See [`troubleshooting.md`](troubleshooting.md) for common local-mode
problems, and [`incident-runbook.md`](incident-runbook.md) for the
full incident response flow.

## Deployment

### Prerequisites

New to Henchmen? Follow [`deploy-gcp.md`](deploy-gcp.md) first — it
covers initial GCP project setup, bootstrap, Terraform apply, and
image push in a linear sequence.

If you're coming back to an already-provisioned stack, you'll need:

- `gcloud` CLI authenticated (`gcloud auth application-default login`)
- Docker running
- Terraform `>= 1.7` (only if you're re-applying infra)
- A GitHub classic personal access token (`repo` scope) stored in Secret Manager
- Slack tokens in Secret Manager (if you've wired Slack)

### Build and Push Containers

All containers are built from the repo root using the context-relative Dockerfile paths.

```bash
# Set variables
PROJECT_ID="${PROJECT_ID}"   # your GCP project ID
REGION="us-central1"
ENV="dev"
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-${ENV}"
TAG="$(git rev-parse --short HEAD)"   # or a release version

# Authenticate Docker with Artifact Registry
gcloud auth configure-docker ${REGION}-docker.pkg.dev

# Build and push all containers
for svc in mastermind operative dispatch forge; do
  docker build -f containers/${svc}/Dockerfile -t ${REGISTRY}/${svc}:${TAG} .
  docker push ${REGISTRY}/${svc}:${TAG}
done
```

Prebuilt release images are also published to `ghcr.io`; see
[Prebuilt images](deploy-gcp.md#prebuilt-images).

### Container Base Images

All containers use `python:3.14-slim-bookworm`, pinned by digest. The Mastermind and Operative containers additionally install Node.js 24 LTS and pnpm 9 (required for running lint and type checks on Node.js target repositories); Node is copied from the matching `bookworm-slim` image so its glibc matches the Python base. The Forge container installs only git. The Dispatch container installs bash for its entrypoint script.

### Deploy Infrastructure with Terraform

```bash
cd terraform/environments/dev

# First time only: identity values go in the git-ignored terraform.tfvars
# (project_id, github_owner, github_default_repo, container_image_tag, ...).
cp dev.auto.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars
terraform init -backend-config=bucket=henchmen-tfstate-${PROJECT_ID}-dev

# Every time
terraform plan -out=dev.tfplan
terraform apply dev.tfplan
```

Variables come from `dev.auto.tfvars` (committed sizing) and `terraform.tfvars`
(your identity and image tag), so no `-var` flags are needed.

**Terraform owns the full Cloud Run configuration.** Every service's image,
environment variables and Secret Manager mounts are declared in the
`cloud-run-services` module. An apply does not strip those secrets, but it does
remove anything added by hand with `gcloud run services update`
(`--set-env-vars`, `--set-secrets`, `--image`). Put permanent changes in
Terraform. The mounts it declares:

| Service | Secrets mounted (env var ← secret) |
|---------|-----------------------------------|
| Mastermind | `GITHUB_TOKEN`, `SLACK_BOT_TOKEN`, `HENCHMEN_METRICS_AUTH_TOKEN` |
| Dispatch | `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_APP_TOKEN`, `JIRA_API_TOKEN`, `HENCHMEN_METRICS_AUTH_TOKEN`, `DISPATCH_API_TOKEN` |
| Forge | `GITHUB_TOKEN`, `HENCHMEN_METRICS_AUTH_TOKEN` |
| Operative (Lairs) | `GITHUB_TOKEN` (attached by LairManager when it creates each job) |

Each maps to `henchmen-${ENV}-<name>` in Secret Manager (for example
`GITHUB_TOKEN` ← `henchmen-dev-github-token`).

Vertex AI RAG Engine uses the service account's Vertex AI IAM roles, so no separate API key secret is required.

### Update a Single Cloud Run Service

The durable way is to push a new tag and set `container_image_tag` in
`terraform.tfvars`, then `terraform apply` — that updates all services and the
operative image tag Mastermind launches lairs with in one step.

For a quick redeploy of one service between applies:

```bash
gcloud run services update henchmen-dev-mastermind \
  --image=${REGISTRY}/mastermind:${TAG} \
  --region=${REGION} \
  --project=${PROJECT_ID}
```

The next `terraform apply` sets the image back to `container_image_tag`.

### Populate Secrets

```bash
# GitHub token
echo -n "ghp_YourTokenHere" | gcloud secrets versions add henchmen-dev-github-token --data-file=-

# Slack bot token
echo -n "xoxb-YourTokenHere" | gcloud secrets versions add henchmen-dev-slack-bot-token --data-file=-

# Dispatch API bearer token (POST /api/v1/tasks returns 401 in staging/prod until this is set)
openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add henchmen-dev-dispatch-api-token --data-file=-
```

Dispatch treats the placeholder value Terraform seeds into
`henchmen-<env>-dispatch-api-token` as "no token", so the route stays closed in
staging and prod until a real version is added.

Secrets are mounted as environment variables with `version = "latest"`, which
Cloud Run resolves when an instance starts. New lairs and newly started
instances pick up a rotated value; instances already running keep the old one
until they are replaced — redeploy the service after rotating.

## Environment Variables

### Settings Configuration

All settings are managed via `src/henchmen/config/settings.py` using `pydantic-settings`. Environment variables use the `HENCHMEN_` prefix (case-insensitive). `.env.example` documents every commonly used one.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HENCHMEN_GCP_PROJECT_ID` | Yes | -- | GCP project ID |
| `HENCHMEN_GCP_REGION` | No | `us-central1` | GCP region |
| `HENCHMEN_ENVIRONMENT` | No | `dev` | `dev`, `staging`, or `prod` |
| `HENCHMEN_FIRESTORE_DATABASE` | No | `(default)` | Firestore database name |
| `HENCHMEN_VERTEX_AI_MODEL_COMPLEX` | No | `gemini-2.5-pro` | Vertex AI model for the `default/complex` tier (`implement_fix`, `implement_feature`) |
| `HENCHMEN_VERTEX_AI_MODEL_LIGHT` | No | `gemini-2.5-flash` | Vertex AI model for the `default/light` tier (planning, classification, reranking) |
| `HENCHMEN_VERTEX_AI_MODEL_REASONING` | No | `gemini-3.1-pro` | Vertex AI model for the `default/reasoning` tier (`fix_tests`, `analyze_goal`) |
| `HENCHMEN_LAIR_DEFAULT_CPU` | No | `4` | CPU for operative jobs (Terraform injects `lair_cpu`) |
| `HENCHMEN_LAIR_DEFAULT_MEMORY` | No | `8Gi` | Memory for operative jobs (Terraform injects `lair_memory`) |
| `HENCHMEN_LAIR_DEFAULT_TIMEOUT` | No | `1800` | Job timeout (seconds) when a node sets none |
| `HENCHMEN_LAIR_OPERATIVE_IMAGE_TAG` | No | `latest` | Operative image tag or digest lairs run (Terraform injects `container_image_tag`) |
| `HENCHMEN_LAIR_SERVICE_ACCOUNT` | No | `sa-<env>-operative@<project>` | Service account lairs run as |
| `HENCHMEN_METRICS_AUTH_TOKEN` | Staging/prod | `` | Bearer token for `/metrics/*` and `/api/v1/metrics/summary` |
| `HENCHMEN_DISPATCH_API_TOKEN` | Staging/prod | `` | Bearer token for `POST /api/v1/tasks` (also read as `DISPATCH_API_TOKEN`) |
| `HENCHMEN_PUBSUB_OIDC_AUDIENCE` | Staging/prod | `` | Expected OIDC audience on Pub/Sub pushes (Terraform sets `henchmen-<env>-<service>`) |
| `HENCHMEN_GITHUB_DEFAULT_REPO` | No | `` | Default target repository (owner/repo format) |

### Runtime Secrets

Cloud Run mounts these from Secret Manager under their bare names. They are
**not** a separate configuration path: `Settings` accepts each bare name as an
alias for the corresponding `HENCHMEN_` field, so the same code reads a Cloud
Run secret mount in production and a `HENCHMEN_`-prefixed value from
`.env.local` locally. When both are present the `HENCHMEN_` name wins.

| Mounted variable | Settings field | Used by |
|------------------|----------------|---------|
| `GITHUB_TOKEN` | `github_token` | Mastermind, Forge, Operative |
| `SLACK_BOT_TOKEN` | `slack_bot_token` | Mastermind, Dispatch |
| `SLACK_SIGNING_SECRET` | `slack_signing_secret` | Dispatch |
| `SLACK_APP_TOKEN` | `slack_app_token` | Dispatch |
| `JIRA_API_TOKEN` | `jira_api_token` | Dispatch, Operative |
| `DISPATCH_API_TOKEN` | `dispatch_api_token` | Dispatch (`POST /api/v1/tasks` bearer token; empty is open in dev with a warning, 401 in staging/prod) |
| `HENCHMEN_METRICS_AUTH_TOKEN` | `metrics_auth_token` | Mastermind (`/metrics/*` and `/api/v1/metrics/summary` bearer token; Terraform also mounts it on Dispatch and Forge) |

### Operative-Specific Variables (injected by LairManager)

Besides the `HENCHMEN_*` configuration it forwards, LairManager sets this
runtime contract on every operative job:

| Variable | Description |
|----------|-------------|
| `TASK_ID` | UUID of the parent task |
| `NODE_ID` | Scheme node being executed (e.g., `implement_fix`) |
| `SCHEME_ID` | Scheme definition ID (e.g., `bugfix_standard`) |
| `LAIR_ID` | Job ID (`lair-{task_id[:8]}-{node_id}-{suffix}`) |
| `MODEL_NAME` | Model tier for the node (e.g., `default/complex`); the operative's LLM provider resolves it |
| `REPO_URL` | Target repository (owner/repo format) |
| `BRANCH` | Branch to clone: the feature branch for fix/retry nodes, otherwise the base branch (default `main`) |
| `TASK_TITLE` | Task title (truncated to 200 chars) |
| `TASK_DESCRIPTION` | Task description (truncated to 16,000 chars) |
| `DOSSIER_URI` | Object-store URI of the serialized dossier, when one was uploaded |

### Pub/Sub Topics (auto-configured)

Topic names are derived from the environment: `henchmen-{env}-{topic-name}`. They do not need to be set manually unless overriding defaults.

## Monitoring

### Cloud Logging Queries

Services log through Python `logging` with a component prefix (`[MASTERMIND]`,
`[SCHEME]`, ...). Those lines arrive in Cloud Logging as `textPayload`, with
token-shaped secrets redacted in Mastermind and the Operative. Metric-style
events (`task.completed`, `cost.exceeded`, watchdog runs) are written as JSON
lines by `observability/structured_logging.py` and arrive as `jsonPayload`
(`jsonPayload.metric_name`, `jsonPayload.metric_labels`).

**All Mastermind activity for a specific task:**
```
resource.type="cloud_run_revision"
resource.labels.service_name="henchmen-dev-mastermind"
textPayload=~"task-id-prefix"
```

**Scheme execution (DAG walking):**
```
resource.type="cloud_run_revision"
textPayload=~"\\[SCHEME\\]"
```

**Lair provisioning and completion:**
```
resource.type="cloud_run_revision"
textPayload=~"\\[LAIR\\]"
```

**Operative tool calls and progress:**
```
resource.type="cloud_run_job"
textPayload=~"\\[OPERATIVE\\]|\\[TOOL\\]"
```

**Dossier building:**
```
resource.type="cloud_run_revision"
textPayload=~"\\[DOSSIER\\]"
```

**PR creation:**
```
resource.type="cloud_run_revision"
textPayload=~"\\[CREATE_PR\\]"
```

**Forge CI results:**
```
resource.type="cloud_run_revision"
resource.labels.service_name="henchmen-dev-forge"
textPayload=~"\\[FORGE\\]"
```

**CI failure auto-fix loop:**
```
resource.type="cloud_run_revision"
textPayload=~"\\[CI-LOOP\\]"
```

**Metric events:**
```
jsonPayload.metric_name="cost.exceeded"
```

### Key Log Patterns

| Pattern | Component | Meaning |
|---------|-----------|---------|
| `[MASTERMIND] Starting task processing: {id}` | Mastermind | Task received and processing begun |
| `[MASTERMIND] Task {id} completed with status:` | Mastermind | Task finished (check status) |
| `[SCHEME] Dispatching agentic node '{id}' to Lair` | SchemeExecutor | Agentic node being sent to a Lair |
| `[SCHEME] Lair {id} completed with status:` | SchemeExecutor | Lair finished execution |
| `[SCHEME] Node {id} hit max retries` | SchemeExecutor | Node exhausted its execution budget (2) |
| `[SCHEME] {type} PASSED/FAILED for task {id}` | SchemeExecutor | Deterministic lint/test result |
| `[SCHEME] Lair provisioning failed for node {id}` | SchemeExecutor | Job creation failed |
| `[LAIR] Creating lair {id} for task {id} node {id}` | LairManager | Cloud Run Job being created |
| `[LAIR] Execution started: {name}` | LairManager | Job execution launched |
| `[OPERATIVE] git_commit succeeded — stopping agent loop` | OperativeAgent | Agent successfully committed changes |
| `[OPERATIVE] Nudge at step {n}` | OperativeAgent | Agent pushed from reading to editing |
| `[OPERATIVE] Pushed branch {name} to origin` | bootstrap | Changes pushed to GitHub |
| `[TOOL] {name}({args})` | OperativeAgent | Tool call with arguments |
| `[TOOL] {name} -> {result}` | OperativeAgent | Tool call result (truncated) |
| `[DOSSIER] Retrieved {n} semantic chunks from RAG Engine` | MastermindAgent | RAG chunks from Vertex AI RAG Engine (`henchmen-code`) |
| `[CREATE_PR] PR created: {url}` | SchemeExecutor | Pull request opened |
| `[FORGE] CI PASSED/FAILED for {url}` | Forge | CI check result |
| `[CI-LOOP] Result: {result}` | Mastermind | CI auto-fix loop outcome |

### Firestore Task Tracking

All task executions are persisted to the `task_executions` Firestore collection. Each document contains:

- `task_id`, `title`, `source`, `scheme_id`, `task_payload`
- `created_at`, `completed_at`, `final_status`
- `execution_state` (`running`, `completed`, `escalated`, `stalled`), `current_node_id`, `last_heartbeat`, `recovery_attempts`
- `pr_url`, `pr_number`, `ci_passed`
- `nodes_executed` (list of node IDs)
- `total_input_tokens`, `total_output_tokens`, `total_model_calls`, `total_tool_calls`
- `estimated_cost_usd`, `wall_clock_seconds`
- `node_metrics` (per-node breakdown: tokens, cost, duration, status)
- `files_changed`, `confidence_score`, `rag_chunks_retrieved`
- `ci_fix_attempts`, `ci_fix_in_progress`
- `escalation_reason`, `escalation_node`
- `expires_at` (30 days after creation; `POST /api/v1/cleanup` deletes expired documents, up to 100 per call — Cloud Scheduler calls it in staging/prod)

### Metrics API

Mastermind serves the metrics router at `/metrics` (under `henchmen serve`:
`http://localhost:8000/mastermind/metrics`). Every request needs
`Authorization: Bearer $HENCHMEN_METRICS_AUTH_TOKEN`; with no token configured
the endpoints are open in dev and return 401 in staging and prod. Responses
carry telemetry only — never task content.

- `GET /metrics/summary?days=7` -- Aggregated metrics: `tasks_total`, `tasks_completed`, `tasks_escalated`, `tasks_ci_passed` / `_failed` / `_pending`, `ci_pass_rate` (null when no CI result has landed), total and average cost, average wall clock, token totals, average confidence, `by_scheme`
- `GET /metrics/tasks?days=7` -- Recent task execution records (ids, statuses, timestamps, numeric telemetry)
- `GET /metrics/tasks/{task_id}` -- One task execution record
- `GET /metrics/prometheus?days=7` -- OpenMetrics gauges `henchmen_tasks_completed_window`, `henchmen_tasks_escalated_window`, `henchmen_cost_usd_window` and, once CI data exists, `henchmen_ci_pass_rate`, each labelled `window_days`. Needs `pip install -e ".[observability]"`; returns 503 without it. See `docs/images/metrics-sample.txt`.

Mastermind also serves `GET /api/v1/metrics/summary?days=7`, a dashboard view
with a different shape (`success_rate`, `escalation_rate`, `cost_by_model`,
`escalation_reasons`). It applies the same bearer-token rules as `/metrics`:

```bash
curl -H "Authorization: Bearer $HENCHMEN_METRICS_AUTH_TOKEN" \
  "http://localhost:8000/mastermind/api/v1/metrics/summary?days=7"
```

Both work under `henchmen serve` and docker compose: the single-process server
runs each mounted service's startup and shutdown, so Mastermind registers its
metrics router (and Dispatch connects the Slack bot) exactly as it does on
Cloud Run.

### Merge Queue State

The `merge_queue` Firestore collection holds merge claims (`pending -> merging -> merged | failed`).
Nothing in Henchmen enqueues into it today — every PR is merged by a human — so
the Forge's scheduled tick (`/api/v1/process-queue`) only expires `merging`
claims older than their TTL and reports the queue depth. An empty collection is
normal.

## Troubleshooting

### Pub/Sub 401 / 403 Errors

**Symptom:** Push subscriptions show 401 or 403 when delivering to Cloud Run services, and tasks never reach Mastermind.

Push subscriptions authenticate as `sa-{env}-pubsub-push` with a fixed OIDC
audience of `henchmen-{env}-{service}` (for example `henchmen-dev-mastermind`),
which Terraform also registers on the service as a custom audience.

- **403 from the Cloud Run edge:** the push service account lacks `roles/run.invoker` on the service. Terraform grants it; re-apply, or grant it by hand:
  ```bash
  gcloud run services add-iam-policy-binding henchmen-dev-mastermind \
    --member="serviceAccount:sa-dev-pubsub-push@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/run.invoker" \
    --region=${REGION}
  ```
- **401 from the application:** the token's audience does not match `HENCHMEN_PUBSUB_OIDC_AUDIENCE` on the receiving service, or the sender is not in `HENCHMEN_PUBSUB_OIDC_ALLOWED_EMAILS`. Compare:
  ```bash
  gcloud pubsub subscriptions describe henchmen-dev-task-intake-sub \
    --format="value(pushConfig.oidcToken.audience,pushConfig.oidcToken.serviceAccountEmail)"
  ```
  with the service's environment. Both are managed by Terraform, so a mismatch usually means a hand edit — re-apply.

### Lair Provisioning Failures

**Symptom:** `[SCHEME] Lair provisioning failed for node {id}: ...`

**Common causes:**

1. **Permission denied on Cloud Run Jobs API:** The Mastermind service account needs `roles/run.developer` to create and run jobs, plus `roles/iam.serviceAccountUser` on the operative service account. Terraform grants both; if one was removed:
   ```bash
   gcloud projects add-iam-policy-binding ${PROJECT_ID} \
     --member="serviceAccount:sa-dev-mastermind@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/run.developer"
   ```

2. **Image not found:** The operative image URI is built from settings: `{region}-docker.pkg.dev/{project}/henchmen-{env}/operative:{HENCHMEN_LAIR_OPERATIVE_IMAGE_TAG}`. Verify the tag exists:
   ```bash
   gcloud artifacts docker images list ${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/operative --include-tags
   ```

3. **Secret access denied:** Lairs mount `henchmen-{env}-github-token` as `GITHUB_TOKEN`. The operative service account needs `roles/secretmanager.secretAccessor` on it (Terraform grants it):
   ```bash
   gcloud secrets add-iam-policy-binding henchmen-dev-github-token \
     --member="serviceAccount:sa-dev-operative@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/secretmanager.secretAccessor"
   ```

4. **Job ID:** Cloud Run Job IDs are capped at 63 characters. LairManager builds `lair-{task_id[:8]}-{node_id}` (underscores to hyphens, truncated to 56) plus a 6-character random suffix, so long node IDs are truncated rather than rejected.

**Dev mode behavior:** In dev, a provisioning failure on an implementation node is treated as a simulated pass so the rest of the pipeline can be exercised; `fix_lint` and `fix_tests` never simulate. In staging and prod every provisioning failure fails the node.

### OOM Kills

**Symptom:** Operative containers are killed with exit code 137 or Cloud Run reports memory limit exceeded.

**Diagnosis:** Check Cloud Run Job logs for `Killed` or `OOMKilled` messages.

**Fixes:**

1. **Increase Lair memory:** Every lair gets `HENCHMEN_LAIR_DEFAULT_MEMORY`, which Terraform sets from `lair_memory` (dev 4Gi, staging 8Gi). Raise `lair_memory` in `terraform.tfvars` and apply.

2. **Increase Mastermind memory:** The Mastermind itself runs at 4Gi (set in the `cloud-run-services` module). If it OOMs during dossier building for large repos, raise the limit there.

3. **Reduce context size:** The operative pre-reads up to 5 relevant files (4,000 chars each, 20,000 tokens total). Tool results over 10,000 characters and messages over 64,000 characters are truncated.

4. **Node.js dependency install:** The `npm ci` or `pnpm install` step during workspace initialization can consume significant memory for large Node.js projects.

### Operative Timeouts

**Symptom:** The operative reports `timed_out`, or the task escalates after a node's timeout.

**Context:** The operative reserves a 120-second buffer for branch push after the agent loop finishes, so the effective agent loop timeout is `node.timeout_seconds - 120` (never less than 60 seconds). A timed-out node stays `timed_out`; it is never upgraded to `completed`.

**Fixes:**

1. **Increase node timeout:** Edit the scheme definition to increase `timeout_seconds` on the agentic node.
2. **Reduce the step budget:** A lower `max_steps` / `step_budget` forces the agent to work more efficiently.
3. **Improve dossier quality:** Better pre-fetched context means fewer exploration steps needed.

### Silent Failure Scan Blocking PRs

**Symptom:** The Forge PR comment shows `silent_failure_scan` failed despite lint and tests passing.

**Cause:** The SilentFailureDetector found critical patterns in the diff (empty catch blocks, bare except/pass, hardcoded secrets).

**Resolution:** Review the Forge CI comment on the PR for specific findings. Only `critical` severity findings cause failures. Fix the flagged patterns in the code.

### Stale CI Fix Loops

**Symptom:** `ci_fix_attempts` is at 2 and the task is stuck in `escalated`.

**Context:** The CI failure auto-fix loop allows max 2 retry attempts. A deduplication flag (`ci_fix_in_progress`) prevents concurrent fix attempts for the same task.

**Resolution:**
1. Check the Firestore document for the task to see `ci_fix_attempts` and `ci_fix_in_progress`.
2. If `ci_fix_in_progress` is stuck at `true`, clear it manually in Firestore.
3. Review the PR and fix remaining CI issues manually.

### Terraform Module Dependencies

The shared composition in `terraform/environments/root/main.tf` declares:

```
bootstrap -> networking, iam, secrets, artifact-registry, cloud-build, observability
bootstrap + iam -> data-stores
bootstrap + networking + iam + secrets + data-stores + artifact-registry
    -> cloud-run-services -> pubsub
                          -> scheduler
    -> cloud-run-lairs
```

`cloud_run_services` must be applied before `pubsub` because the push subscription endpoints reference the Cloud Run service URLs. If you see errors about unknown service URLs, ensure `cloud_run_services` is applied first.
