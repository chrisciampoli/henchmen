# Operations Guide

> If you are self-hosting Henchmen (docker-compose, `henchmen serve`, SQLite,
> filesystem, Ollama): most of this guide assumes a GCP deployment. Treat the
> GCP names as a legend, not as a requirement. The self-hosted equivalents:
>
> - Cloud Run services -> containers in docker-compose (or processes spawned by `henchmen serve`)
> - Firestore -> SQLite at `henchmen_dev.db` (or filesystem JSON under `./henchmen-data/`)
> - Pub/Sub -> in-memory broker or local HTTP forwarder
> - Secret Manager -> `.env.local`
> - Cloud Scheduler -> a local cron hitting `/api/v1/watchdog`
> - Cloud Logging -> `docker logs` or the single stdout stream of `henchmen serve`
>
> See `docs/incident-runbook.md` ("Self-Hosted / Non-GCP Operations") for the
> full mapping and `docs/troubleshooting.md` for common self-hosted problems.

## Deployment

### Prerequisites

- GCP project with billing enabled
- Terraform >= 1.7 installed
- Docker installed
- `gcloud` CLI authenticated (`gcloud auth application-default login`)
- GitHub personal access token or GitHub App credentials
- Slack bot token (for Slack integration)
- Vertex AI RAG Engine corpus (named `henchmen-code`) for semantic search

### Build and Push Containers

All containers are built from the repo root using the context-relative Dockerfile paths.

```bash
# Set variables
PROJECT_ID="${PROJECT_ID}"   # your GCP project ID
REGION="us-central1"
ENV="dev"
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-${ENV}"

# Build all containers
docker build -f containers/mastermind/Dockerfile -t ${REGISTRY}/mastermind:latest .
docker build -f containers/operative/Dockerfile -t ${REGISTRY}/operative:latest .
docker build -f containers/dispatch/Dockerfile -t ${REGISTRY}/dispatch:latest .
docker build -f containers/forge/Dockerfile -t ${REGISTRY}/forge:latest .

# Authenticate Docker with Artifact Registry
gcloud auth configure-docker ${REGION}-docker.pkg.dev

# Push all images
docker push ${REGISTRY}/mastermind:latest
docker push ${REGISTRY}/operative:latest
docker push ${REGISTRY}/dispatch:latest
docker push ${REGISTRY}/forge:latest
```

### Container Base Images

All containers use `python:3.12-slim`. The Mastermind and Operative containers additionally install Node.js 20 and pnpm 9 (required for running lint and type checks on Node.js target repositories). The Forge container installs only git. The Dispatch container installs bash for its entrypoint script.

### Deploy Infrastructure with Terraform

```bash
cd terraform/environments/dev

# Initialize (first time only)
terraform init

# Plan changes
terraform plan -var="project_id=${PROJECT_ID}" -var="region=${REGION}" -var="environment=dev"

# Apply
terraform apply -var="project_id=${PROJECT_ID}" -var="region=${REGION}" -var="environment=dev"
```

**Important:** After `terraform apply`, you must manually restore secrets that Terraform resets. Terraform manages the Cloud Run service definitions but strips environment variable secret references on each apply. After applying, verify that these secrets are present on the Cloud Run services:

| Service | Required Secrets |
|---------|-----------------|
| Mastermind | `SLACK_BOT_TOKEN`, `GITHUB_TOKEN` |
| Dispatch | `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_APP_TOKEN` |
| Forge | `GITHUB_TOKEN` |
| Operative (Lairs) | `GITHUB_TOKEN` (injected by LairManager at job creation time) |

Vertex AI RAG Engine uses the service account's Vertex AI IAM roles, so no separate API key secret is required.

### Update a Single Cloud Run Service

To deploy a new version of a single service without a full Terraform apply:

```bash
# Example: update Mastermind
gcloud run services update henchmen-dev-mastermind \
  --image=${REGISTRY}/mastermind:latest \
  --region=${REGION} \
  --project=${PROJECT_ID}
```

### Populate Secrets

```bash
# GitHub token
echo -n "ghp_YourTokenHere" | gcloud secrets versions add henchmen-dev-github-token --data-file=-

# Slack bot token
echo -n "xoxb-YourTokenHere" | gcloud secrets versions add henchmen-dev-slack-bot-token --data-file=-
```

## Environment Variables

### Settings Configuration

All settings are managed via `src/henchmen/config/settings.py` using `pydantic-settings`. Environment variables use the `HENCHMEN_` prefix (case-insensitive).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HENCHMEN_GCP_PROJECT_ID` | Yes | -- | GCP project ID |
| `HENCHMEN_GCP_REGION` | No | `us-central1` | GCP region |
| `HENCHMEN_ENVIRONMENT` | No | `dev` | `dev`, `staging`, or `prod` |
| `HENCHMEN_FIRESTORE_DATABASE` | No | `(default)` | Firestore database name |
| `HENCHMEN_VERTEX_AI_MODEL_COMPLEX` | No | `gemini-2.5-pro` | Default Gemini model for complex tasks (`implement_fix`, `implement_feature`) |
| `HENCHMEN_VERTEX_AI_MODEL_LIGHT` | No | `gemini-2.5-flash` | Default Gemini model for lightweight tasks (`verify_changes`, `plan_implementation`) |
| `HENCHMEN_VERTEX_AI_MODEL_REASONING` | No | `gemini-3.1-pro` | Default Gemini model for reasoning-heavy tasks (`fix_tests`, `analyze_goal`) |
| `HENCHMEN_LAIR_DEFAULT_CPU` | No | `4` | Default CPU for operative containers |
| `HENCHMEN_LAIR_DEFAULT_MEMORY` | No | `8Gi` | Default memory for operative containers |
| `HENCHMEN_LAIR_DEFAULT_TIMEOUT` | No | `1800` | Default operative timeout (seconds) |
| `HENCHMEN_GITHUB_DEFAULT_REPO` | No | `` | Default target repository (owner/repo format) |

### Runtime Secrets (not in Settings)

These are injected directly as environment variables by Cloud Run secret references:

| Variable | Used By | Source |
|----------|---------|--------|
| `GITHUB_TOKEN` | Mastermind, Forge, Operative | Secret Manager |
| `SLACK_BOT_TOKEN` | Mastermind, Dispatch | Secret Manager |
| `SLACK_SIGNING_SECRET` | Dispatch | Secret Manager |
| `SLACK_APP_TOKEN` | Dispatch | Secret Manager |

### Operative-Specific Variables (injected by LairManager)

These are set when the Mastermind creates a Cloud Run Job for an operative:

| Variable | Description |
|----------|-------------|
| `TASK_ID` | UUID of the parent task |
| `NODE_ID` | Scheme node being executed (e.g., `implement_fix`) |
| `SCHEME_ID` | Scheme definition ID (e.g., `bugfix_standard`) |
| `LAIR_ID` | Cloud Run Job ID |
| `MODEL_NAME` | Vertex AI model to use |
| `REPO_URL` | Target repository (owner/repo format) |
| `BRANCH` | Base branch to clone (default: `main`) |
| `TASK_TITLE` | Task title (truncated to 200 chars) |
| `TASK_DESCRIPTION` | Task description (truncated to 500 chars) |

### Pub/Sub Topics (auto-configured)

Topic names are automatically derived from the environment: `henchmen-{env}-{topic-name}`. They do not need to be set manually unless overriding defaults.

## Monitoring

### Cloud Logging Queries

The system uses structured print statements with component prefixes. Use these Cloud Logging filters to track task execution:

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

### Key Log Patterns

| Pattern | Component | Meaning |
|---------|-----------|---------|
| `[MASTERMIND] Starting task processing: {id}` | Mastermind | Task received and processing begun |
| `[MASTERMIND] Task {id} completed with status:` | Mastermind | Task finished (check status) |
| `[SCHEME] Dispatching agentic node '{id}'` | SchemeExecutor | Agentic node being sent to a Lair |
| `[SCHEME] Lair {id} completed with status:` | SchemeExecutor | Lair finished execution |
| `[SCHEME] Node {id} hit max retries` | SchemeExecutor | Node exhausted retry budget (2) |
| `[SCHEME] {type} PASSED/FAILED for task {id}` | SchemeExecutor | Deterministic lint/test result |
| `[LAIR] Creating job {id}` | LairManager | Cloud Run Job being created |
| `[LAIR] Execution started: {name}` | LairManager | Job execution launched |
| `[OPERATIVE] git_commit succeeded` | OperativeAgent | Agent successfully committed changes |
| `[OPERATIVE] Phase nudge at step {n}` | OperativeAgent | Agent pushed from reading to editing |
| `[OPERATIVE] Pushed branch {name}` | bootstrap | Changes pushed to GitHub |
| `[TOOL] {name}({args})` | OperativeAgent | Tool call with arguments |
| `[TOOL] {name} -> {result}` | OperativeAgent | Tool call result (truncated) |
| `[DOSSIER] Retrieved {n} semantic chunks` | MastermindAgent | RAG chunks from Vertex AI RAG Engine (`henchmen-code`) |
| `[CREATE_PR] PR created: {url}` | SchemeExecutor | Pull request opened |
| `[FORGE] CI PASSED/FAILED for {url}` | Forge | CI check result |
| `[CI-LOOP] Result: {result}` | Mastermind | CI auto-fix loop outcome |

### Firestore Task Tracking

All task executions are persisted to the `task_executions` Firestore collection. Each document contains:

- `task_id`, `title`, `source`, `scheme_id`
- `created_at`, `completed_at`, `final_status`
- `pr_url`, `pr_number`, `ci_passed`
- `nodes_executed` (list of node IDs)
- `total_input_tokens`, `total_output_tokens`, `total_model_calls`, `total_tool_calls`
- `estimated_cost_usd`, `wall_clock_seconds`
- `node_metrics` (per-node breakdown: tokens, cost, duration, status)
- `files_changed`, `confidence_score`
- `ci_fix_attempts`, `ci_fix_in_progress`
- `expires_at` (30-day TTL)

### Metrics API

The Mastermind exposes a metrics API at `/metrics`:

- `GET /metrics/summary?days=7` -- Aggregated metrics: task count, CI pass rate, total cost, average cost per task, token usage, average confidence, breakdown by scheme
- `GET /metrics/tasks?days=7` -- List of recent task execution records

### Merge Queue State

The `merge_queue` Firestore collection tracks PRs waiting to be merged:

- States: `pending -> merging -> merged` (or `failed`)
- FIFO ordering by `created_at`
- Serialization guard: only one merge in progress at a time

## Troubleshooting

### Pub/Sub 403 Errors

**Symptom:** Push subscriptions return 403 when delivering to Cloud Run services.

**Cause:** The Pub/Sub push service account does not have `roles/run.invoker` on the target Cloud Run service.

**Fix:**
```bash
# Grant the push SA permission to invoke Mastermind
gcloud run services add-iam-policy-binding henchmen-dev-mastermind \
  --member="serviceAccount:sa-dev-mastermind@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker" \
  --region=${REGION}
```

All push subscriptions use OIDC tokens with the Mastermind service account. Verify with:
```bash
gcloud pubsub subscriptions describe henchmen-dev-task-intake-sub
```
The `pushConfig.oidcToken.serviceAccountEmail` must match a service account with `run.invoker` on the target.

### Lair Provisioning Failures

**Symptom:** `[SCHEME] Lair provisioning failed for node {id}: ...`

**Common causes:**

1. **Permission denied on Cloud Run Jobs API:** The Mastermind service account needs `roles/run.developer` to create and run jobs.
   ```bash
   gcloud projects add-iam-policy-binding ${PROJECT_ID} \
     --member="serviceAccount:sa-dev-mastermind@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/run.developer"
   ```

2. **Image not found:** The operative image URI is built from settings: `{region}-docker.pkg.dev/{project}/henchmen-{env}/operative:latest`. Verify the image exists:
   ```bash
   gcloud artifacts docker images list ${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-dev/operative
   ```

3. **Secret access denied:** Operative Lairs reference `GITHUB_TOKEN` from Secret Manager. The operative service account needs `roles/secretmanager.secretAccessor`:
   ```bash
   gcloud secrets add-iam-policy-binding henchmen-dev-github-token \
     --member="serviceAccount:sa-dev-operative@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role="roles/secretmanager.secretAccessor"
   ```

4. **Job ID too long:** Cloud Run Job IDs are capped at 63 characters. The format is `lair-{task_id[:8]}-{node_id}` with underscores replaced by hyphens. Long node IDs may cause issues.

**Dev mode behavior:** In dev mode, lair provisioning failures are treated as simulated passes so the rest of the pipeline can be tested end-to-end. In production, failures are fail-closed.

### OOM Kills

**Symptom:** Operative containers are killed with exit code 137 or Cloud Run reports memory limit exceeded.

**Diagnosis:** Check Cloud Run Job logs for `Killed` or `OOMKilled` messages.

**Fixes:**

1. **Increase Lair memory:** The default is 8Gi. For large repositories or long-running tasks, the LairManager automatically scales to 4 vCPU / 8Gi for nodes with timeout > 300s. To increase the default:
   ```
   HENCHMEN_LAIR_DEFAULT_MEMORY=16Gi
   ```

2. **Increase Mastermind memory:** The Mastermind itself runs at 4Gi (set in Terraform). If it OOMs during dossier building for large repos, update the Cloud Run service limits.

3. **Reduce context size:** The operative pre-reads the top 10 most relevant files (capped at 5000 chars each). Large repos with many relevant files can cause context to grow. The tool result truncation limit is 30K chars.

4. **Node.js dependency install:** The `npm ci` or `pnpm install` step during workspace initialization can consume significant memory for large Node.js projects.

### Operative Timeouts

**Symptom:** Task escalates with `Agent exceeded timeout of {n}s`.

**Context:** The operative reserves a 120-second buffer for branch push after the agent loop finishes. So the effective agent loop timeout is `node.timeout_seconds - 120`.

**Fixes:**

1. **Increase node timeout:** Edit the scheme definition to increase `timeout_seconds` on the agentic node.
2. **Reduce max_steps:** A lower step limit forces the agent to work more efficiently.
3. **Improve dossier quality:** Better pre-fetched context means fewer exploration steps needed.

### Silent Failure Scan Blocking PRs

**Symptom:** CI fails with `silent_failure_scan: FAILED` despite lint and tests passing.

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

The module dependency chain is:

```
bootstrap -> networking -> iam -> secrets
                                -> artifact-registry
                                -> data-stores
                         -> cloud-run-services -> pubsub
                                               -> cloud-run-lairs
                                               -> scheduler
                         -> cloud-build
                         -> observability
                         -> vertex-ai
```

`cloud_run_services` must be deployed before `pubsub` because the push subscription endpoints reference the Cloud Run service URLs. If you see errors about unknown service URLs, ensure `cloud_run_services` is applied first.
