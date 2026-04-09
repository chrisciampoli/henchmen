# Troubleshooting

This guide covers the problems self-hosters hit most often. Each entry is
symptom / diagnosis / fix so you can scan it in 30 seconds.

For deeper operational questions (log locations, task state recovery,
adding new LLM models to the price map), see
`docs/incident-runbook.md` under "Self-Hosted / Non-GCP Operations".

---

## 1. `henchmen serve` fails with "operative image not found"

**Symptom:** Startup aborts with an error like `operative image
henchmen-operative:local not found` or the first task fails with a
container-create error.

**Diagnosis:** The ephemeral operative runs inside a Docker container. The
image has to exist locally before you can dispatch a task.

**Fix:**

```bash
henchmen build-operative            # default
henchmen build-operative --no-cache # force a clean rebuild
```

First build takes ~2 minutes on a warm network. Docker Desktop must be
running.

---

## 2. Slack events arrive but nothing happens

**Symptom:** Slack shows "Request URL verified" (or the Socket Mode connection
is up), but posting a task does not create a PR. No mastermind log lines.

**Diagnosis:** Either the signing secret is wrong and the dispatch handler is
silently rejecting payloads, or `HENCHMEN_PROVIDER` is set to a value that
disables the Slack intake (e.g. `gcp` without the Socket Mode token).

**Fix:**

1. Confirm `SLACK_SIGNING_SECRET` in `.env.local` matches the one in the
   Slack app config exactly (no whitespace, no trailing newline).
2. Confirm `HENCHMEN_PROVIDER=local` (or `HENCHMEN_DISPATCH_SLACK=true` on
   GCP).
3. Restart `henchmen serve` after changing `.env.local` -- settings are
   cached.

---

## 3. Webhook returns 401 Unauthorized

**Symptom:** GitHub or Slack webhook deliveries show red with a 401.

**Diagnosis:** Signing-secret mismatch. Henchmen verifies every webhook
signature and rejects on mismatch. In production the verifier is fail-closed.

**Fix:**

1. Rotate the secret in the upstream (GitHub app settings, Slack app config).
2. Update `.env.local` or Secret Manager with the new value.
3. Restart the service so the new secret is picked up.
4. Redeliver the failed webhook from the upstream UI.

---

## 4. Ollama model produces empty responses

**Symptom:** The operative container runs, calls the LLM, but the commit is
empty or the PR has no file changes. Logs show `tool_calls: []` or a single
text response with no function calls.

**Diagnosis:** Many small open models (7B and below) cannot reliably produce
OpenAI-style tool calls. Henchmen needs structured tool calls to drive the
Arsenal.

**Fix:** Switch to a stronger model.

```bash
ollama pull qwen2.5-coder:7b   # minimum recommended
ollama pull qwen2.5-coder:14b  # more reliable
```

Set `HENCHMEN_LLM_OLLAMA_MODEL=qwen2.5-coder:7b` in `.env.local`. For
production-quality results, switch to OpenAI or Anthropic.

---

## 5. Forge CI hangs

**Symptom:** A PR is opened, but the status never moves from `ci_pending`.

**Diagnosis:** Forge is waiting on a CI result that will never arrive
(misconfigured provider, stuck worker), or the task is starved behind others.

**Fix:**

```bash
curl http://localhost:8000/metrics/summary | jq .tasks_ci_pending
curl http://localhost:8000/metrics/summary | jq .by_scheme
```

If pending count is stuck, tail the forge logs and look for the task ID.
For a self-hosted quick-unstick, clear the CI flag in the document store
(see `docs/incident-runbook.md`).

---

## 6. `HENCHMEN_PROVIDER` vs `HENCHMEN_LLM_PROVIDER`

**Symptom:** You set `HENCHMEN_PROVIDER=local` and expected Ollama, but
Henchmen is calling OpenAI (or vice versa).

**Diagnosis:** `HENCHMEN_PROVIDER` selects the default backend family
(message broker, document store, object store, container orchestrator, LLM,
CI). `HENCHMEN_LLM_PROVIDER` is a narrower override that only touches the LLM
layer. When both are set, the specific override wins.

**Fix:** Decide explicitly. Typical local dev:

```bash
HENCHMEN_PROVIDER=local                # everything local
HENCHMEN_LLM_PROVIDER=openai           # except LLM
HENCHMEN_OPENAI_API_KEY=sk-...
```

---

## 7. "Mastermind skips `run_tests` on non-JS/TS repos"

**Symptom:** Your Python or Go repo gets a PR, but `run_tests` shows as
skipped in the mastermind logs even though the repo has tests.

**Diagnosis:** Expected behaviour. The inline `run_tests` handler in
Mastermind is hardcoded for pnpm/npm-based JS/TS monorepos and short-circuits
on anything else. Full CI still runs through Forge once the PR lands.

**Fix:** Not a bug. If you want native support for your language, see the
`run_tests` handler in `src/henchmen/mastermind/handlers/` and add a branch.

---

## 8. Local DB file growing too large

**Symptom:** `henchmen_dev.db` is hundreds of MB, SQLite writes are slow.

**Diagnosis:** Henchmen writes one row per task execution plus per-node
metrics. Over weeks of heavy use the file bloats.

**Fix:**

```bash
sqlite3 henchmen_dev.db "DELETE FROM task_executions WHERE created_at < datetime('now','-30 days');"
sqlite3 henchmen_dev.db "VACUUM;"
```

Or simply stop the process, delete `henchmen_dev.db`, and restart. The file
is purely cache / state and gets rebuilt.

---

## 9. Cost ceiling exceeded

**Symptom:** Tasks are escalating with `cost_ceiling_exceeded` or
`over_budget`.

**Diagnosis:** The per-task cost cap is configurable. Henchmen fails closed
rather than burn through your wallet.

**Fix:**

1. See the current cap and total: `curl http://localhost:8000/metrics/summary`
2. Raise the ceiling in `.env.local`:

   ```bash
   HENCHMEN_COST_CEILING_USD_PER_TASK=2.50
   ```

3. Restart the service.

Also check `by_scheme` in the summary response to see which scheme is
responsible for the overage.

---

## 10. Pub/Sub OIDC 401 errors

**Symptom (GCP only):** Push subscription logs show 401s delivering to
Cloud Run. Tasks never reach Mastermind.

**Diagnosis:** The push subscription's OIDC audience does not match the
Cloud Run service URL. Terraform apply can reset this.

**Fix:** Set `HENCHMEN_PUBSUB_OIDC_AUDIENCE` to the exact Cloud Run URL:

```bash
HENCHMEN_PUBSUB_OIDC_AUDIENCE=https://henchmen-dev-mastermind-xxxxx.run.app
```

Then redeploy, or update the subscription directly (see
`docs/incident-runbook.md` under "Pub/Sub 403").

---

## 11. Tests fail locally with ImportError

**Symptom:** `pytest tests/integration/` blows up with `ModuleNotFoundError`
for `google.cloud.pubsub_v1` or similar.

**Diagnosis:** The integration test conftest imports GCP / AWS SDKs even
though the tests patch them with in-memory fakes. Those modules still need to
be importable.

**Fix:**

```bash
pip install -e ".[dev-integration]"
```

Or skip integration tests for the fast loop:

```bash
pytest tests/unit/
```

---

## 12. Operative container bootstraps but no code is written

**Symptom:** The operative container shows "execution started", runs for
a while, and exits cleanly, but the PR is empty.

**Diagnosis:** Almost always the model is not tool-calling. The agent loop
generates reasoning text but never invokes `code_edit`, so nothing touches
the filesystem.

**Fix:**

1. Confirm your model supports function-calling (OpenAI, Anthropic, Gemini,
   or Qwen 2.5 Coder are the known-good options).
2. Look for `tool_calls_by_name` in the telemetry line. An empty dict
   confirms the diagnosis.
3. Switch models or raise the tool-call temperature.

---

## 13. Force-push is refused

**Symptom:** Operative logs say `force-push refused: HENCHMEN_ALLOW_FORCE_PUSH is false`.

**Diagnosis:** Henchmen will not force-push by default, because doing so on
`main` is a known footgun. The gate is off in every environment out of the
box.

**Fix:** Only enable this if you fully understand the blast radius:

```bash
HENCHMEN_ALLOW_FORCE_PUSH=true
```

Prefer creating a new branch or resetting the target branch locally.

---

## 14. Windows line-ending issues in `.env.local`

**Symptom:** On Windows, `.env.local` is loaded but a secret looks subtly
wrong (e.g. `GITHUB_TOKEN` fails 401 even though the value is correct).

**Diagnosis:** Windows line endings (`\r\n`) get baked into the last
character of each value. pydantic-settings does not strip them.

**Fix:** Open `.env.local` in an editor that supports "Save with LF"
(VS Code: bottom-right status bar -> CRLF -> LF -> Save). Or run:

```bash
python -c "open('.env.local','wb').write(open('.env.local','rb').read().replace(b'\r\n', b'\n'))"
```

---

## 15. docker-compose containers show "unhealthy"

**Symptom:** `docker compose ps` lists one or more services as `unhealthy`
and they never become ready.

**Diagnosis:** The recent compose rewrite added healthchecks and
`depends_on: condition: service_healthy` so services wait for Ollama. If
Ollama takes longer than the healthcheck period to pull its first model,
dependents may flap.

**Fix:**

1. Check `docker logs henchmen-ollama` for `error pulling model`.
2. Pre-pull the model once: `docker exec henchmen-ollama ollama pull qwen2.5-coder:7b`.
3. Restart the stack: `docker compose up -d`.

Mentioned here for historical context -- the default healthchecks should
now be reliable.
