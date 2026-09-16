# Troubleshooting

This guide covers the problems self-hosters hit most often. Each entry is
symptom / diagnosis / fix so you can scan it in 30 seconds.

Start with `henchmen doctor`: it reads the same `Settings` the services use and
checks Docker, git, the operative image and every credential you configured.
`henchmen config --only-set` shows the values that differ from the defaults,
with credentials masked.

For deeper operational questions (log locations, task state recovery,
adding new LLM models to the price table), see
`docs/incident-runbook.md` under "Self-Hosted / Non-GCP Operations".

In local mode every service runs inside `henchmen serve` on port 8000, mounted
under `/dispatch`, `/mastermind` and `/forge`; `docker compose up` runs the same
process on the same port.

---

## 1. The first task fails because `henchmen-operative:local` does not exist

**Symptom:** `henchmen serve` starts normally, but the first agentic node fails.
The server log shows a Docker error such as
`Unable to find image 'henchmen-operative:local'` followed by `pull access denied`.

**Diagnosis:** The ephemeral operative runs inside a Docker container. The
image has to exist locally before a task can dispatch one. `henchmen serve`
does not check for it or build it; `henchmen doctor` reports it missing.

**Fix:**

```bash
henchmen build-operative            # default
henchmen build-operative --no-cache # force a clean rebuild
```

The first build takes a few minutes (longer on a slow connection). Docker
Desktop must be running.

---

## 2. Slack mentions do nothing

**Symptom:** You @mention the bot, but no task is created and no mastermind
log lines appear.

**Diagnosis:** The bot connects over Socket Mode, which needs both the bot
token and the app-level token. When either is missing the Dispatch service logs
`Slack Socket Mode disabled: set HENCHMEN_SLACK_BOT_TOKEN and HENCHMEN_SLACK_APP_TOKEN to enable it`
at startup. This is the same under `henchmen serve` and `docker compose up`,
which run Dispatch's startup inside the single process, so look for that line
(or `[dispatch] Service started`) in the server log. If you use the HTTP Events API (`/webhooks/slack`) instead, a
wrong signing secret makes Dispatch reject every event with 401.

**Fix:**

1. Run `henchmen doctor` — it validates both Slack tokens against the Slack API.
2. Confirm `HENCHMEN_SLACK_BOT_TOKEN` (`xoxb-...`) and
   `HENCHMEN_SLACK_APP_TOKEN` (`xapp-...`, with `connections:write`) are set,
   and that the app subscribes to the `app_mention` bot event.
3. For the HTTP Events API only: confirm `HENCHMEN_SLACK_SIGNING_SECRET`
   matches the Slack app's signing secret exactly (no whitespace, no trailing
   newline).
4. Restart the process after changing `.env.local` -- settings are cached.

---

## 3. Webhook returns 401 Unauthorized

**Symptom:** GitHub, Jira or Slack webhook deliveries show red with a 401.

**Diagnosis:** Signing-secret mismatch, or no secret configured in staging or
prod. Henchmen verifies every webhook signature and rejects on mismatch; in
staging and prod a missing secret rejects every delivery.

**Fix:**

1. Rotate the secret in the upstream (the GitHub repository's webhook
   settings, the Jira webhook, or the Slack app config).
2. Update `HENCHMEN_GITHUB_WEBHOOK_SECRET`, `HENCHMEN_JIRA_WEBHOOK_SECRET` or
   `HENCHMEN_SLACK_SIGNING_SECRET` in `.env.local` or Secret Manager.
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

## 5. Forge CI result never arrives

**Symptom:** A PR is opened, but the task's `ci_passed` stays empty and no
Henchmen CI comment appears on the PR.

**Diagnosis:** Forge never received the `forge-request`, or it failed before
publishing a `forge-result` (clone failure, crash, misconfigured provider).

**Fix:**

```bash
TOKEN="Authorization: Bearer $HENCHMEN_METRICS_AUTH_TOKEN"   # optional only in dev with no token set
curl -H "$TOKEN" http://localhost:8000/mastermind/metrics/summary | jq .tasks_ci_pending
curl -H "$TOKEN" http://localhost:8000/mastermind/metrics/summary | jq .by_scheme
```

If the pending count is stuck, search the server log for `[FORGE]` lines with
the PR URL. To close out a task by hand, update its `task_executions` record
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

## 7. `run_lint` / `run_tests` fail with "could not detect the project stack"

**Symptom:** The task escalates and the mastermind log shows
`lint failed — could not detect the project stack for owner/repo` (or the same
for `tests`).

**Diagnosis:** The lint and test gates detect the target repository's stack
from its manifest files (`src/henchmen/utils/stack_detector.py`): Python,
Rust, Go, Java (Maven or Gradle), and Node (pnpm or npm). When none match, the
gate fails closed and the task escalates rather than opening a PR nobody
checked. This is expected, not a skip. The handlers live in
`src/henchmen/mastermind/scheme_executor/handlers.py`.

**Fix:** Make sure the manifest (`pyproject.toml`, `package.json`, `go.mod`,
`Cargo.toml`, `pom.xml`, `build.gradle`) is at the repository root. To support
another language, add a stack to `stack_detector.py` in a pull request.

Once the PR is open, Forge runs its own CI on it. A Forge run is `passed` only
when every check ran and passed. If a check cannot run — for example the
target's tests need a tool the Forge image does not have — the run is reported
as `incomplete`: the PR comment flags the skipped checks and the task is not
recorded as a CI pass.

---

## 8. Local DB file growing too large

**Symptom:** The SQLite store is hundreds of MB and writes are slow.

**Diagnosis:** Henchmen writes one `task_executions` row per task (with
per-node metrics). Rows past their 30-day `expires_at` are only deleted when
something calls the cleanup endpoint, which nothing does in local mode. The
store lives at
`~/.henchmen/henchmen_<environment>.db` unless `HENCHMEN_LOCAL_SQLITE_PATH`
says otherwise. Every collection is a table of `(id, data)` rows where `data`
is the JSON document.

**Fix:** While `henchmen serve` is running, delete expired rows (repeat until
`expired_cleaned` is 0 — it removes up to 100 per call):

```bash
curl -X POST http://localhost:8000/mastermind/api/v1/cleanup
```

Or stop `henchmen serve` and prune directly:

```bash
DB=~/.henchmen/henchmen_dev.db
sqlite3 "$DB" "DELETE FROM task_executions WHERE json_extract(data, '$.created_at') < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 days');"
sqlite3 "$DB" "VACUUM;"
```

Or delete the file (and its `-wal` / `-shm` companions) and restart. It holds
only task state and history, which is rebuilt as new tasks run.

---

## 9. Cost ceiling exceeded

**Symptom:** A task escalates and the node result says
`Cost budget exceeded: cumulative $X + estimated $Y > ceiling $Z (raise HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD)`.

**Diagnosis:** Before each agentic node the scheme executor adds the node's
estimated cost to what the task has spent so far and fails the node if the
total would pass the per-task ceiling. The operative enforces the same
ceiling inside its loop. Henchmen fails closed rather than burn through your
wallet.

**Fix:**

1. Check the current ceiling: `henchmen config | grep COST_CEILING` (default `6.0`).
2. Raise it in `.env.local`:

   ```bash
   HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD=10.0
   ```

3. Restart the service.

Which scheme is spending the most, and which model (the metrics bearer token
is required whenever `HENCHMEN_METRICS_AUTH_TOKEN` is set, and always in
staging and prod):

```bash
TOKEN="Authorization: Bearer $HENCHMEN_METRICS_AUTH_TOKEN"
curl -H "$TOKEN" http://localhost:8000/mastermind/metrics/summary | jq .by_scheme
curl -H "$TOKEN" http://localhost:8000/mastermind/api/v1/metrics/summary | jq .cost_by_model
```

---

## 10. Pub/Sub push 401 errors

**Symptom (GCP only):** Push subscription deliveries fail with 401, and tasks
never reach Mastermind.

**Diagnosis:** The OIDC token's audience does not match
`HENCHMEN_PUBSUB_OIDC_AUDIENCE` on the receiving service. Terraform sets both
sides to the same fixed value, `henchmen-{env}-{service}` (for example
`henchmen-dev-mastermind`) — not the service URL. A mismatch almost always
means one side was edited by hand.

**Fix:** Re-run `terraform apply` for the environment. To check by hand:

```bash
gcloud pubsub subscriptions describe henchmen-dev-task-intake-sub \
  --format="value(pushConfig.oidcToken.audience)"
```

must equal `HENCHMEN_PUBSUB_OIDC_AUDIENCE` on `henchmen-dev-mastermind`. A 403
instead of a 401 means the push service account lacks `roles/run.invoker`; see
`docs/operations.md` under "Pub/Sub 401 / 403 Errors".

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
generates reasoning text but never invokes an edit tool (`file_edit`,
`file_write`), so nothing touches the filesystem.

**Fix:**

1. Confirm your model supports function-calling (OpenAI, Anthropic, Gemini,
   or Qwen 2.5 Coder are the known-good options).
2. Look for `tool_calls_by_name` in the operative's telemetry line. No edit
   tools in it confirms the diagnosis.
3. Switch to a model with reliable tool calling (see #4).

---

## 13. Force-push is refused

**Symptom:** An operative's `git_force_push` call returns
`git_force_push is disabled. Set HENCHMEN_ALLOW_FORCE_PUSH=1 in the operative environment to enable.`

**Diagnosis:** Force-push is not part of the standard Henchmen workflow and is
off by default in every environment. Even when enabled it refuses protected
branches (`main`, `master`, `develop`, `trunk`, and names starting with
`release`, `rel/` or `stable`) and requires an explicit branch name.

**Fix:** Only enable this if you fully understand the blast radius:

```bash
HENCHMEN_ALLOW_FORCE_PUSH=true
```

The setting is forwarded to operative containers. Prefer creating a new branch
instead.

---

## 14. Windows line-ending issues in `.env.local`

**Symptom:** On Windows, `.env.local` is loaded but a secret looks subtly
wrong (e.g. `HENCHMEN_GITHUB_TOKEN` fails with 401 even though the value is
correct).

**Diagnosis:** Windows line endings (`\r\n`) get baked into the last
character of each value. pydantic-settings does not strip them.

**Fix:** Open `.env.local` in an editor that supports "Save with LF"
(VS Code: bottom-right status bar -> CRLF -> LF -> Save). Or run:

```bash
python -c "open('.env.local','wb').write(open('.env.local','rb').read().replace(b'\r\n', b'\n'))"
```

---

## 15. docker compose shows "unhealthy"

**Symptom:** `docker compose ps` lists `henchmen` or `henchmen-ollama` as
`unhealthy` and it never becomes ready.

**Diagnosis:** The `henchmen` service waits for the Ollama healthcheck
(`depends_on: condition: service_healthy`) and has its own healthcheck on
`http://localhost:8000/health`. If Ollama is slow to start, or `henchmen serve`
exits (for example on a configuration error in `.env.local`), the stack flaps.

**Fix:**

1. Check `docker logs henchmen-ollama` and `docker logs henchmen` for the error.
2. Pre-pull the model once: `docker exec henchmen-ollama ollama pull qwen2.5-coder:7b`.
3. Restart the stack: `docker compose up -d`.

## 16. A first task on a Python repo fails the test gate over a missing dependency

**Symptom:** The guided setup Console's "first task" step (or any task on a
Python project) fails at `run_tests` / `fix_tests` with an import error for a
package that is listed in the project's own `requirements.txt` or
`pyproject.toml` — not a Henchmen problem, just a dependency the test run
never installed.

**Diagnosis:** Neither the local gate container nor the cloud test-runner path
installs a Python project's dependencies before running its tests (tracked for
a future phase; see `src/henchmen/console/steps/first_task.py`'s
`DEPENDENCY_NOTE`). A task whose tests need anything beyond the standard
library cannot pass the test gate yet, regardless of the change's own
correctness.

**Fix:** For the guided setup's first task, pick a documentation-only sample
(the Console's own suggestions are chosen for exactly this reason) or a small
change with no test dependencies. For a Python repository more generally, keep
using it for tasks whose tests already pass with no extra install, or
`henchmen chat`/CLI dispatch with a change scoped the same way, until dependency
installation lands.

## 17. The Console's setup steps can't reach a provider through a corporate proxy

**Symptom:** `henchmen serve`'s setup Console times out or fails to reach
GitHub, Slack, Jira, or an LLM provider's API from a machine that only has
network access through an HTTP(S) proxy, even though `HTTP_PROXY`/`HTTPS_PROXY`
are set in the environment.

**Diagnosis:** The Console's step routers (AI provider, GitHub, Slack, Jira)
build their own `httpx` clients with `trust_env=False`, so they never pick up
`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` from the process environment — this is
deliberate (it keeps a step's checks from silently depending on ambient
environment state), but it means there is currently no way to route setup
traffic through a proxy. An explicit, `Settings`-backed proxy option is a
follow-up, not yet implemented.

**Fix:** None yet if the setup machine truly has no direct route. Run
`henchmen init`/`henchmen doctor` from a machine that can reach the provider
APIs directly, or complete `.env.local` by hand and skip the affected Console
steps.
