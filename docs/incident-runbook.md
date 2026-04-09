# Incident Runbook -- Henchmen

> Note: The bulk of this runbook is written for operators running Henchmen on
> GCP. If you are self-hosting on docker-compose, SQLite, or a single VM, read
> the "Self-Hosted / Non-GCP Operations" section first -- it explains how each
> `gcloud` / Firestore instruction maps to your environment.

## Self-Hosted / Non-GCP Operations

Henchmen runs in two deployment shapes:

1. GCP-managed (Cloud Run + Firestore + Pub/Sub + Cloud Scheduler).
2. Self-hosted (docker-compose or bare `henchmen serve`, SQLite / filesystem
   backends, in-memory broker or a local HTTP forwarder).

The checks below cover what to do in shape #2 -- no `gcloud`, no Cloud
Logging, no Cloud Scheduler.

### Finding logs

If you started the stack with docker-compose:

```bash
docker logs -f henchmen-mastermind
docker logs -f henchmen-dispatch
docker logs -f henchmen-forge
docker logs -f henchmen-ollama
```

If you started it with `henchmen serve` (single-process), all logs are on
stdout of that process. Redirect to a file for persistence:

```bash
henchmen serve 2>&1 | tee henchmen.log
```

### Inspecting task state (local document store)

The local document store persists to `henchmen_dev.db` (SQLite) in the working
directory. You can poke at it directly:

```bash
sqlite3 henchmen_dev.db
sqlite> .tables
sqlite> SELECT id, title, status, updated_at FROM tasks ORDER BY updated_at DESC LIMIT 10;
sqlite> SELECT id, status, ci_passed FROM task_executions ORDER BY created_at DESC LIMIT 10;
```

If you chose the filesystem document store, each document is a JSON file
under `./henchmen-data/<collection>/<id>.json`. Open them with any editor.

### Recovering a stuck task without gcloud

Symptom: a task is stuck in `dispatched` or `in_progress` and nothing is
advancing it. Without Firestore you cannot use the GCP fix; instead, patch
the document store directly:

```bash
sqlite3 henchmen_dev.db
sqlite> UPDATE tasks SET status = 'failed', updated_at = datetime('now') WHERE id = '<task-id>';
sqlite> .quit
```

For a filesystem store, open the JSON file and change the `status` field.
Restart `henchmen serve` or the mastermind container so in-memory state
aligns with the on-disk update.

### Missing Cloud Scheduler cron

The GCP deployment uses Cloud Scheduler to POST to `/api/v1/watchdog` on a
schedule (stuck-task sweep, merge queue tick). Self-hosted users should call
this endpoint themselves, either manually or from a local cron:

```bash
curl -X POST http://localhost:8000/api/v1/watchdog
```

A reasonable crontab entry:

```
*/5 * * * * curl -sS -X POST http://localhost:8000/api/v1/watchdog >/dev/null
```

### Adding a new LLM model to the price map

The cost tracker keeps a static price map in
`src/henchmen/observability/tracker.py` (look for `_PRICE_MAP`). If you add a
model that is not listed, token usage will still be recorded but the cost
column in `/metrics/summary` will read `$0.00`. To fix:

1. Open `src/henchmen/observability/tracker.py`.
2. Add an entry to `_PRICE_MAP` with the exact model string your provider
   returns (e.g. `"gpt-4o-mini-2024-07-18"`) and per-1M-token input / output
   rates in USD.
3. Restart the process.
4. Confirm with `curl http://localhost:8000/metrics/summary | jq .total_cost_usd`.

## Alert Conditions

| Alert | Trigger | Severity |
|-------|---------|----------|
| Operative Timeout | Cloud Run Job exceeds `lair_default_timeout` (1800s) | High |
| Escalation Loop | Same task escalated >2 times within 1 hour | Critical |
| Pub/Sub 403 | Push subscription returns 403 (missing OIDC audience) | Critical |
| Dead Letter Queue Growth | `henchmen-{env}-dead-letter` message count >10 in 5 min | High |
| CI Build Failure | Cloud Build returns non-zero for >3 consecutive PRs | Medium |
| Forge Stuck | Merge queue entry in `merging` status >15 min | High |
| Dispatch Unhealthy | `/health` returns non-200 or response time >5s | Critical |

## Quick Diagnosis

### Operative Timeout

**Symptoms:** Task stuck in `in_progress`, operative Cloud Run Job shows `TIMED_OUT` status.

1. Check the operative job logs:
   ```bash
   gcloud run jobs executions list --job=henchmen-{env}-lair-template --project=${PROJECT_ID} --region=us-central1
   gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="henchmen-{env}-lair-template"' --project=${PROJECT_ID} --limit=50
   ```

2. Look for the telemetry report (logged just before timeout):
   - `context_tokens_at_end` -- if very high (>500k), the operative ran out of context window
   - `steps_used` vs `max_steps` -- if equal, the operative hit its step limit
   - `tool_calls_by_name` -- check if stuck in a read loop (excessive `file_read` calls)

3. Check if the model endpoint is responding:
   ```bash
   gcloud logging read 'jsonPayload.message=~"VertexAI" AND severity>=ERROR' --project=${PROJECT_ID} --limit=20
   ```

**Common fixes:**
- If context window exhaustion: reduce `max_steps` on the scheme node or add file filtering to the dossier
- If model timeout: check Vertex AI quota and regional status for Gemini (e.g., `us-central1`). Henchmen uses Gemini on Vertex AI exclusively -- no Claude/Anthropic routing.
- If stuck in tool loop: review the scheme's `instruction_template` for missing phase constraints

### Escalation Loop

**Symptoms:** Slack channel flooded with escalation messages for the same task.

1. Query Firestore for the task:
   ```bash
   gcloud firestore documents list --collection=tasks --filter="id={task_id}" --project=${PROJECT_ID}
   ```

2. Check the scheme executor logs for retry exhaustion:
   ```bash
   gcloud logging read 'jsonPayload.task_id="{task_id}" AND jsonPayload.message=~"escalat"' --project=${PROJECT_ID} --limit=20
   ```

3. Verify the task state machine isn't cycling:
   - Valid terminal states: `completed`, `failed`, `escalated`
   - If state is toggling between `in_progress` and `dispatched`, there is a re-dispatch bug

**Common fixes:**
- Manually set the task status to `escalated` in Firestore to break the loop
- If the scheme itself is causing re-dispatch, check `SchemeExecutor` retry logic -- max retries should fail-closed

### Pub/Sub 403 (Silent Authentication Failure)

**Symptoms:** Messages published successfully but push subscriptions never deliver. No errors in publisher logs. Subscriber logs show 403.

1. Check subscription configuration:
   ```bash
   gcloud pubsub subscriptions describe henchmen-{env}-{topic}-sub --project=${PROJECT_ID}
   ```

2. Verify OIDC audience matches the Cloud Run service URL:
   ```bash
   gcloud run services describe henchmen-{env}-mastermind --project=${PROJECT_ID} --region=us-central1 --format="value(status.url)"
   ```
   The `pushConfig.oidcToken.audience` in the subscription must match this URL exactly.

3. Check the push subscription dead letter policy:
   ```bash
   gcloud pubsub subscriptions describe henchmen-{env}-{topic}-sub --project=${PROJECT_ID} --format="yaml(deadLetterPolicy)"
   ```

**Common fixes:**
- Update the subscription OIDC audience:
  ```bash
  gcloud pubsub subscriptions update {sub_name} \
    --push-auth-service-account={sa}@${PROJECT_ID}.iam.gserviceaccount.com \
    --push-auth-token-audience={cloud_run_url} \
    --project=${PROJECT_ID}
  ```
- If Terraform recently ran, it may have reset the audience. Re-apply the correct value.

### Dead Letter Queue Growth

**Symptoms:** Messages accumulating in `henchmen-{env}-dead-letter` topic.

1. Pull messages to inspect:
   ```bash
   gcloud pubsub subscriptions pull henchmen-{env}-dead-letter-sub --project=${PROJECT_ID} --limit=5 --auto-ack
   ```

2. Check the original topic's subscription for delivery failures:
   ```bash
   gcloud pubsub subscriptions describe henchmen-{env}-task-intake-sub --project=${PROJECT_ID} --format="yaml(deadLetterPolicy)"
   ```

**Common fixes:**
- If messages are malformed: check Dispatch normalizer output
- If subscriber is crashing: check Cloud Run service logs for the receiving service
- If authentication: see "Pub/Sub 403" above

### Forge Stuck (Merge Queue)

**Symptoms:** PR not being merged despite passing CI.

1. Check merge queue in Firestore:
   ```bash
   gcloud firestore documents list --collection=merge_queue --filter="status=merging" --project=${PROJECT_ID}
   ```

2. Check Forge service logs:
   ```bash
   gcloud logging read 'resource.labels.service_name="henchmen-{env}-forge" AND severity>=WARNING' --project=${PROJECT_ID} --limit=30
   ```

3. Verify GitHub API access:
   ```bash
   gcloud secrets versions access latest --secret=github-app-private-key --project=${PROJECT_ID} | head -1
   ```

**Common fixes:**
- Mark the stuck entry as `failed` in Firestore to unblock the queue
- If GitHub token expired: rotate the GitHub App installation token
- Restart the Forge service: `gcloud run services update henchmen-{env}-forge --project=${PROJECT_ID} --region=us-central1`

## Escalation Procedures

| Level | Who | When |
|-------|-----|------|
| L1 | On-call engineer | Any alert fires |
| L2 | Maintainer | L1 cannot resolve in 30 min, or Critical severity |
| L3 | GCP Support | Infrastructure-level issues (Vertex AI outage, Cloud Run quota) |

## Common Fixes Quick Reference

| Issue | Fix |
|-------|-----|
| Service returning 503 | Redeploy: `gcloud run services update henchmen-{env}-{svc} --image=...` |
| Env vars missing after TF apply | Re-set secrets: check Terraform output, manually re-apply secret env vars |
| Operative image stale | Rebuild + push + update both service and lair template |
| Task stuck in `dispatched` | Check Mastermind logs; manually transition to `failed` if needed |
| Firestore quota exceeded | Check Firestore usage dashboard; consider adding indexes |
| High LLM costs | Check model tiering -- ensure `fix_lint` is deterministic, `verify_changes` uses Flash |
