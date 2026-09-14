# Incident Runbook -- Henchmen

> Note: The bulk of this runbook is written for operators running Henchmen on
> GCP. If you are self-hosting with `henchmen serve` or docker compose, read
> the "Self-Hosted / Non-GCP Operations" section first -- it explains how each
> `gcloud` / Firestore instruction maps to your environment.

## Self-Hosted / Non-GCP Operations

Henchmen runs in two deployment shapes:

1. GCP-managed (Cloud Run + Firestore + Pub/Sub + Cloud Scheduler).
2. Self-hosted: a single `henchmen serve` process (docker compose runs the
   same process in the `henchmen` container), with a SQLite document store,
   filesystem object store and in-memory broker.

The checks below cover what to do in shape #2 -- no `gcloud`, no Cloud
Logging, no Cloud Scheduler. All services share port 8000 under `/dispatch`,
`/mastermind` and `/forge`.

### Finding logs

If you started the stack with docker compose:

```bash
docker logs -f henchmen          # Dispatch + Mastermind + Forge
docker logs -f henchmen-ollama
```

If you started it with `henchmen serve`, all logs are on stdout of that
process. Redirect to a file for persistence:

```bash
henchmen serve 2>&1 | tee henchmen.log
```

Operative containers log to their own Docker containers (`docker ps -a`).

### Inspecting task state (local document store)

The local document store is SQLite at `~/.henchmen/henchmen_<environment>.db`
(override with `HENCHMEN_LOCAL_SQLITE_PATH`). Each collection is a table with
two columns, `id` and `data`, where `data` is the JSON document, so query
fields with `json_extract`:

```bash
sqlite3 ~/.henchmen/henchmen_dev.db
sqlite> .tables
sqlite> SELECT id,
   ...>        json_extract(data, '$.title'),
   ...>        json_extract(data, '$.final_status'),
   ...>        json_extract(data, '$.execution_state'),
   ...>        json_extract(data, '$.ci_passed')
   ...> FROM task_executions
   ...> ORDER BY json_extract(data, '$.created_at') DESC LIMIT 10;
```

There is no filesystem document store; the filesystem backend
(`~/.henchmen/storage`) only holds object-store blobs such as dossiers.

### Recovering a stuck task without gcloud

Symptom: a task's `execution_state` stays `running` and nothing is advancing
it. First try the watchdog (next section), which re-publishes stalled tasks
and escalates them after 3 attempts. To close the task out by hand instead,
stop `henchmen serve` and patch the document:

```bash
sqlite3 ~/.henchmen/henchmen_dev.db
sqlite> UPDATE task_executions
   ...> SET data = json_set(data, '$.final_status', 'escalated',
   ...>                           '$.execution_state', 'escalated')
   ...> WHERE id = '<task-id>';
sqlite> .quit
```

Then start `henchmen serve` again.

### Missing Cloud Scheduler cron

The GCP staging/prod deployment uses Cloud Scheduler to call Mastermind's
`/api/v1/watchdog` (every 5 minutes), `/api/v1/check-dlq` and
`/api/v1/cleanup`, and Forge's `/api/v1/process-queue`. Dev sets
`scheduler_enabled = false`. Self-hosted users should call the watchdog
themselves, either manually or from a local cron:

```bash
curl -X POST http://localhost:8000/mastermind/api/v1/watchdog
```

A reasonable crontab entry:

```
*/5 * * * * curl -sS -X POST http://localhost:8000/mastermind/api/v1/watchdog >/dev/null
```

### Adding a new LLM model to the price map

Token prices live in exactly one place: `PRICE_TABLE` in
`src/henchmen/providers/pricing.py`. A model that is not listed still has its
token usage recorded, but its cost reads `$0.00` and it therefore never trips
the per-task ceiling. Scheme nodes store tier names, and cost is computed after
resolving the tier through the active provider's `Settings` field (for example
`HENCHMEN_OPENAI_MODEL_COMPLEX`), so the entry must match that concrete model.
To fix:

1. Run `henchmen doctor` to see the concrete model each tier resolves to.
2. Open `src/henchmen/providers/pricing.py`.
3. Add a `PRICE_TABLE` entry keyed on the first-party model family id (e.g.
   `"gpt-4o-mini"`), using the `_anthropic`, `_gemini` or `_openai` helper so
   the cache-read and cache-write rates follow that vendor's discount. Dated
   snapshots, Vertex `@` forms and Bedrock ids normalise onto that key
   automatically, so one entry usually covers every spelling.
4. Restart the process.
5. Confirm with
   `curl -H "Authorization: Bearer $HENCHMEN_METRICS_AUTH_TOKEN" http://localhost:8000/mastermind/metrics/summary | jq .total_cost_usd`.

Do not add a second price map anywhere. Cost is always computed through
`estimate_cost` / `estimate_cost_for_settings` from that module.

## Alert Conditions

Terraform (`terraform/modules/observability`) provisions three alert policies,
all on metrics Cloud Run and Pub/Sub emit themselves:

| Alert | Trigger | Severity |
|-------|---------|----------|
| Lair Timeout Alert | A Cloud Run Job execution finished with `result = failed` (timeouts included) | High |
| Dead Letter Queue Alert | `henchmen-{env}-dead-letter-sub` has undelivered messages for 60s | High |
| Henchmen Service Error Rate Alert | Any `henchmen-{env}-*` service returns 5xx over 5 minutes | Critical |

The sections below also cover conditions no alert fires for (escalation loops,
Pub/Sub auth failures); watch for them in logs.

## Quick Diagnosis

### Operative Timeout

**Symptoms:** Lair Timeout Alert fires, or a task's `execution_state` stays
`running` and the operative's job execution shows as failed.

1. Find the lair jobs. Mastermind creates one job per agentic node, named
   `lair-<task_id[:8]>-<node_id>-<suffix>`:
   ```bash
   gcloud run jobs list --project=${PROJECT_ID} --region=us-central1 --filter="metadata.name ~ ^lair-<task_id[:8]>"
   gcloud run jobs executions list --job=<lair-job-name> --project=${PROJECT_ID} --region=us-central1
   gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="<lair-job-name>"' --project=${PROJECT_ID} --limit=50
   ```

2. Look for the telemetry report (logged just before timeout):
   - `context_tokens_at_end` -- if very high (>500k), the operative ran out of context window
   - `steps_used` vs `max_steps` -- if equal, the operative hit its step limit
   - `tool_calls_by_name` -- check if stuck in a read loop (excessive `file_read` calls)

3. Check if the model endpoint is responding:
   ```bash
   gcloud logging read 'resource.type="cloud_run_job" AND severity>=ERROR' --project=${PROJECT_ID} --limit=20
   ```

**Common fixes:**
- If context window exhaustion: reduce the node's step budget or add file filtering to the dossier
- If model timeout: check quota and status for the provider `HENCHMEN_LLM_PROVIDER` selects. On Vertex AI that means Gemini quota in the region (Henchmen never uses Claude on Vertex AI); for Anthropic, OpenAI or Bedrock, check that vendor's status page and rate limits.
- If stuck in tool loop: review the scheme's `instruction_template` for missing phase constraints
- A timed-out operative stays `timed_out`; never mark it completed by hand

### Escalation Loop

**Symptoms:** Slack channel flooded with escalation messages for the same task.

1. Read the task's execution record. There is no `gcloud firestore` command for
   documents; use the Firestore console
   (`https://console.cloud.google.com/firestore/databases/<database>/data/panel/task_executions/<task_id>?project=${PROJECT_ID}`)
   or the Python client:
   ```python
   from google.cloud import firestore

   db = firestore.Client(project="PROJECT_ID", database="DATABASE")
   print(db.collection("task_executions").document("TASK_ID").get().to_dict())
   ```

2. Check the scheme executor logs for retry exhaustion:
   ```bash
   gcloud logging read 'resource.labels.service_name="henchmen-{env}-mastermind" AND textPayload=~"TASK_ID" AND textPayload=~"(escalat|max retries)"' --project=${PROJECT_ID} --limit=20
   ```

3. Verify the task isn't cycling:
   - Terminal `execution_state` values: `completed`, `escalated`
   - `recovery_attempts` climbing means the watchdog keeps re-publishing a task whose heartbeat stops; it escalates after 3

**Common fixes:**
- Set `execution_state` and `final_status` to `escalated` on the `task_executions` document to stop the watchdog re-publishing it
- If the scheme itself is causing re-dispatch, check `SchemeExecutor` retry logic -- max retries should fail-closed

### Pub/Sub Push 401 / 403

**Symptoms:** Messages publish successfully but tasks never reach Mastermind. The Service Error Rate alert does not fire for 401/403, but the subscription's push metrics show failed deliveries and messages end up in the dead-letter topic.

Push subscriptions authenticate as `sa-{env}-pubsub-push` with a fixed OIDC
audience of `henchmen-{env}-{service}` (for example `henchmen-dev-mastermind`),
registered on the service as a custom audience and injected as
`HENCHMEN_PUBSUB_OIDC_AUDIENCE`.

1. Check the subscription configuration:
   ```bash
   gcloud pubsub subscriptions describe henchmen-{env}-{topic}-sub --project=${PROJECT_ID} \
     --format="yaml(pushConfig,deadLetterPolicy)"
   ```

2. Compare `pushConfig.oidcToken.audience` with the receiving service's `HENCHMEN_PUBSUB_OIDC_AUDIENCE`:
   ```bash
   gcloud run services describe henchmen-{env}-mastermind --project=${PROJECT_ID} --region=us-central1 \
     --format="yaml(spec.template.spec.containers[0].env)"
   ```

**Common fixes:**
- **401** (rejected by the application): audience or allowed-email mismatch. Both sides are Terraform-managed; re-run `terraform apply`. To patch a subscription by hand:
  ```bash
  gcloud pubsub subscriptions update henchmen-{env}-{topic}-sub \
    --push-auth-service-account=sa-{env}-pubsub-push@${PROJECT_ID}.iam.gserviceaccount.com \
    --push-auth-token-audience=henchmen-{env}-{service} \
    --project=${PROJECT_ID}
  ```
- **403** (rejected by the Cloud Run edge): `sa-{env}-pubsub-push` lacks `roles/run.invoker` on the service; re-apply Terraform (see `docs/operations.md`).

### Dead Letter Queue Growth

**Symptoms:** Dead Letter Queue Alert fires; messages accumulating in `henchmen-{env}-dead-letter-sub`.

Mastermind's `/api/v1/check-dlq` (Cloud Scheduler, every 15 minutes) pulls up
to 10 dead-lettered messages, escalates the task each one carries, and
acknowledges them. Pulling by hand acknowledges them too, so the check will not
see them.

1. Trigger the check, or inspect messages without acking:
   ```bash
   gcloud pubsub subscriptions pull henchmen-{env}-dead-letter-sub --project=${PROJECT_ID} --limit=5
   ```

2. Check the original topic's subscription for delivery failures:
   ```bash
   gcloud pubsub subscriptions describe henchmen-{env}-task-intake-sub --project=${PROJECT_ID} --format="yaml(deadLetterPolicy)"
   ```

**Common fixes:**
- If messages are malformed: check Dispatch normalizer output
- If subscriber is crashing: check Cloud Run service logs for the receiving service
- If authentication: see "Pub/Sub Push 401 / 403" above

### GitHub Access Failing

**Symptoms:** `create_pr` fails, Forge cannot clone or comment, or operatives cannot push.

1. Check the token Henchmen uses (a classic PAT stored as `henchmen-{env}-github-token`):
   ```bash
   gcloud secrets versions access latest --secret=henchmen-{env}-github-token --project=${PROJECT_ID} \
     | { read -r t; curl -s -H "Authorization: Bearer $t" https://api.github.com/user | jq .login; }
   ```

2. Check Forge service logs:
   ```bash
   gcloud logging read 'resource.labels.service_name="henchmen-{env}-forge" AND severity>=WARNING' --project=${PROJECT_ID} --limit=30
   ```

**Common fixes:**
- If the token expired or lost the `repo` scope: create a new classic PAT and add it as a new version of `henchmen-{env}-github-token`, then redeploy Mastermind and Forge so new instances read it (new lairs pick it up automatically)

## Escalation Procedures

| Level | Who | When |
|-------|-----|------|
| L1 | On-call engineer | Any alert fires |
| L2 | Maintainer | L1 cannot resolve in 30 min, or Critical severity |
| L3 | GCP Support | Infrastructure-level issues (Vertex AI outage, Cloud Run quota) |

## Common Fixes Quick Reference

| Issue | Fix |
|-------|-----|
| Service returning 503 | Redeploy: `terraform apply` with the intended `container_image_tag`, or `gcloud run services update henchmen-{env}-{svc} --image=...` for a quick fix |
| Hand-set env vars gone after TF apply | Expected: Terraform owns service env and secret mounts. Add the value to the `cloud-run-services` module |
| Operative image stale | Push the operative image with the tag in `HENCHMEN_LAIR_OPERATIVE_IMAGE_TAG` (Terraform's `container_image_tag`); new lairs use it immediately |
| Task stuck in `running` | Call `/api/v1/watchdog`; check Mastermind logs; set `execution_state`/`final_status` to `escalated` if needed |
| Firestore quota exceeded | Check Firestore usage dashboard; consider adding indexes |
| High LLM costs | Check `henchmen doctor` for the model each tier resolves to, confirm `HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD`, and compare `by_scheme` in `/metrics/summary`. `fix_lint` and `verify_changes` never call a model |
