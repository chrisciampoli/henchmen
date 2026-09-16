# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Semver policy during 0.x:** While we are on `0.x` releases, minor version
bumps may include breaking changes. We will call them out explicitly in this
changelog under a `Changed` or `Removed` heading so upgraders can plan. Once
we hit `1.0.0`, standard semver rules apply.

## [Unreleased]

The full-review release. A sixteen-dimension audit of the whole repository
produced 490 findings; every critical and high one was independently verified
before being fixed. Three root causes account for most of them: scheme nodes
had been migrated to model *tiers* that only two of five providers resolved,
roughly sixty raw `os.environ` reads bypassed `Settings` so `.env.local` was
silently ignored, and the Dispatch container never ran its own HTTP app.

### Added
- `henchmen init` (alias `henchmen setup`) — an interactive setup wizard. It
  picks the deployment mode, LLM provider and per-tier models from the model
  list your key can actually reach, validates GitHub, Slack and Jira
  credentials against their APIs, lists your Slack channels and joins the one
  you choose, then writes `.env.local` atomically with a backup. Flags:
  `--yes`, `--dry-run`, `--section`, `--env-file`.
- `src/henchmen/providers/tiers.py` — one tier resolver shared by every
  provider, the cost tracker and the CLI. Accepts the friendly provider
  aliases `ollama`, `vertex` and `bedrock`.
- `src/henchmen/providers/pricing.py` — the single token price table, with
  vendor model-id normalisation (dated snapshots, Vertex `@` forms, Bedrock
  ids) and cache-read/cache-write aware cost estimation.
- Per-tier model settings for every provider: `vertex_ai_model_reasoning`
  (Vertex previously had no reasoning tier), `llm_ollama_model_{complex,light,reasoning}`,
  and `bedrock_model_{complex,light,reasoning}`.
- `Settings.operative_env()` — the `HENCHMEN_*` variables an operative
  container needs, so operator-configured limits and tier models actually
  reach it.
- `Settings.validate_for_runtime()` — reports missing API keys, empty tier
  models, non-positive limits, and a missing OIDC audience or metrics token in
  staging and prod, without raising.
- New settings: `llm_chat_model`, `allow_force_push`, `local_serve_port`,
  `local_forward_base_url`, `metrics_auth_token`, `lair_service_account`.
- `henchmen doctor` now builds real `Settings` and probes live credentials
  (GitHub token and repo push access, Slack tokens and channel membership,
  Ollama reachability and pulled models, Anthropic/OpenAI keys, Jira), prints
  the resolved model tiers, and takes `--offline`.
- Slack Socket Mode now starts inside the Dispatch process and joins
  `slack_notification_channel` on startup.
- `henchmen embed <owner/repo> [--full]` re-indexes a repository for semantic
  code search and exits non-zero unless indexing completes. Mastermind consumes
  `embed-request` pushes at `POST /pubsub/embed-request`, with an authenticated
  Terraform push subscription and dead-letter policy, so push-triggered
  re-indexing is no longer published to a topic nothing reads.
- `task_type` (`bugfix`, `feature`, `refactor`) on `POST /api/v1/tasks` and
  `HenchmenTask`; scheme selection honours it after goal keywords. `henchmen chat`
  sends it.
- `henchmen config [--only-set]` prints the effective settings with every
  credential masked.
- Semantic code-search results are reranked with one light-tier LLM call
  (`HENCHMEN_DOSSIER_SEMANTIC_RERANK`, on by default).
- New settings: `dispatch_api_token`, `dispatch_rate_limit_requests`,
  `dispatch_rate_limit_window_seconds`, `dispatch_trust_forwarded_for`,
  `jira_repo_field`, `jira_branch_field`, `forge_ci_timeout_seconds`,
  `dossier_semantic_rerank`, `eval_db_path`, `local_sqlite_path`,
  `local_storage_dir`, `ci_builder_image`, `ci_github_token_secret`,
  `dead_letter_subscription`, `aws_ecs_execution_role_arn`.
- CI validates Terraform (`fmt -check`, `init -backend=false -lockfile=readonly`,
  `validate` for dev and staging); the environments' provider lock files are
  committed. Unit tests run on Python 3.12 and 3.14.
- The Forge image includes Node 24 and pnpm, so JS/TS pull requests' test suites
  actually run.
- `henchmen doctor` warns when a tier's model has no price, since an unpriced
  model can never trip the cost ceiling.
- Firestore composite index on `task_executions` (`execution_state`,
  `last_heartbeat`) for the stalled-task watchdog query.
- Local all-in-one image (`ghcr.io/<owner>/henchmen/local`, built with
  `--target local`), published for amd64 and arm64 with every other image.
- `henchmen serve` setup mode: with `HENCHMEN_DATA_DIR` set and setup
  incomplete, only the Console and `/health` are served. The Console is
  localhost-only — it requires a loopback Host, an Origin whose port matches
  the Host's on state-changing requests and every WebSocket handshake, and a
  signed session cookie on every `/console/api/*` route except
  `/console/api/status`. Session signing keys live at
  `<data dir>/secrets/console-session.key` (mode 0600, at least 32 bytes,
  regenerated if shorter). The Console persists guide progress and applies
  setup by exiting the process with code 75, distinct from uvicorn's own
  startup-failure code (3) and a clean Ctrl+C (0); the local image's
  `--restart unless-stopped` policy is what actually relaunches the container
  into run mode afterward — a bare `henchmen serve` with no restart policy
  just stops. `henchmen serve` prints `Open Henchmen setup: <url>` in setup
  mode and `Open Henchmen: <url>` once running, and fails closed with a readable
  `ERROR:` and exit code 2 on a corrupt `setup-state.json`, an unwritable
  secrets directory, or a non-integer `HENCHMEN_LOCAL_SERVE_PORT`.
- Desktop hardening (guided setup Phase 2A): `config/posture.py` disables every dev-only fail-open path on
  data-directory installs; a whole-app Host allowlist; an internal push token and per-task operative
  tokens in `<data dir>/secrets/`; task-scoped `/mastermind/internal/tasks/{task_id}/...` routes and an
  operative `HttpDocumentStore`; a one-time, per-start setup token and `henchmen console-link`;
  server-recorded setup step completion with the `console/steps` router contract; apply-time validation
  that ignores defaults `henchmen serve` seeded; a generated `HENCHMEN_DISPATCH_API_TOKEN` at apply; a
  needs-attention Console (`/health` → `degraded`) when a completed setup cannot start; status
  `problems` and `services`.
- Settings `local_container_hostname` and `operative_task_token`.
- Settings `local_docker_network` and `operative_image`.
- Setup Console connection steps (`/console/api/steps/*`): AI provider (live
  key check, model list, per-task cost estimate and spending limit), GitHub
  (GitHub App created from a manifest, installation, default repository),
  Slack (manifest link, token checks, channel join and test message), Jira
  (project and custom-field pickers, intake label) and a first task with a
  live timeline. Every write goes through one configuration writer that
  accepts only real settings and never returns a saved secret.
- GitHub App authentication (`henchmen.utils.github_auth`): installation
  tokens minted from the App's key, cached until five minutes before expiry,
  scoped to one repository; every server-side GitHub call uses them, and
  operatives receive only a short-lived token and refresh it before it
  expires (`POST /mastermind/internal/tasks/{task_id}/github-token`). The App
  requests contents, pull requests and issues write plus metadata, checks and
  actions read — never workflows — so a change to `.github/workflows/`
  escalates with a clear message. Without an App, `HENCHMEN_GITHUB_TOKEN`
  works as before.
- Settings `HENCHMEN_GITHUB_APP_ID`, `HENCHMEN_GITHUB_APP_INSTALLATION_ID`,
  `HENCHMEN_GITHUB_APP_PRIVATE_KEY_PATH`, `HENCHMEN_GITHUB_API_URL`,
  `HENCHMEN_GITHUB_WEB_URL`, `HENCHMEN_GITHUB_TOKEN_EXPIRES_AT`, `HENCHMEN_JIRA_INTAKE_LABEL`.
- `henchmen init` and `henchmen doctor` validate AWS Bedrock live, and doctor
  verifies GitHub App token minting.
- `Settings.runtime_notices()` — half-finished setup states that must never
  stop a service from starting, the counterpart of `validate_for_runtime()`.
  A GitHub App created but not yet installed is reported here, so an abandoned
  reconnect cannot take a working install into needs-attention mode on its
  next restart; `henchmen serve` and every service log it and `henchmen doctor`
  shows it as a warning. Token calls on such a configuration still fail closed,
  and every other partly configured App still refuses to start.

### Changed
- Operative containers no longer inherit the GitHub token implicitly from
  settings; LairManager passes exactly one token.
- **Local-orchestrator CI runs in a gate container.** Whenever the effective container orchestrator is
  local Docker, which includes a repository checkout running `HENCHMEN_PROVIDER=local`, Forge CI and the
  `fix_lint` node run in a gate container from the operative image, so both now need Docker and that
  image. Forge runs lint and tests in a single `ci_gate forge` container (one clone, one dependency
  install) under one deadline capped below the in-memory broker's forward timeout, and a re-sent
  forge-request is deduplicated by request id. Its lint uses the Mastermind lint gate's `lint_scope` rules,
  which are stricter than the cloud path's ruff on changed Python files.
- **Credential settings accept two spellings.** `github_token`,
  `slack_bot_token`, `slack_app_token`, `slack_signing_secret`,
  `jira_base_url`, `jira_email` and `jira_api_token` accept both the
  `HENCHMEN_`-prefixed name and the bare name a Cloud Run secret mount injects
  (`GITHUB_TOKEN`, `SLACK_BOT_TOKEN`, ...), with the prefixed name winning.
  The old `*_secret` fields are gone; their env names still work.
- **The Dispatch container runs the FastAPI app.** `containers/dispatch/entrypoint.sh`
  now execs uvicorn instead of a stub health server, so `/api/v1/tasks`,
  `/webhooks/{slack,github,jira}` and `/pubsub/*` exist in every deployment.
  The Slack bot starts from the app lifespan.
- **Vertex light tier defaults to `gemini-2.5-flash`** (was `gemini-2.5-pro`,
  which made the "95% cheaper" light tier cost the same as complex).
- **Anthropic tier defaults are current model ids**: `claude-sonnet-5`,
  `claude-haiku-4-5`, `claude-opus-5`.
- `MODEL_NAME` for an operative defaults to `default/complex` rather than a
  Gemini model name, so a node without an explicit model works on any provider.
- `Settings` validates provider names at construction; a typo fails immediately
  with the valid list instead of deep inside a container.
- The default Ollama model is `qwen2.5-coder:7b` — `llama3.2` cannot reliably
  drive the operative's tool loop.
- `docker-compose.yml` runs a single `henchmen serve` container with the Docker
  socket mounted, so the stack can actually execute a task.
- `henchmen serve` no longer overrides values from `.env.local`.
- `henchmen eval` accepts the documented short form again
  (`henchmen eval --provider X`), plus `--all`, and provider aliases.
- The eval harness moved to `src/henchmen/evals/` so it works from an installed
  wheel; `evals/` keeps re-export shims and the fixtures.
- `pytest`, `ruff` and the `[local]` extra are installed in the mastermind and
  operative images so cloud-mode CI checks and direct-LLM operatives work.
- `src/henchmen/__init__.py` reads `__version__` from package metadata.
- Container images run Python 3.14.7 and Node 24 LTS (Node copied from the
  matching `bookworm-slim` image); GitHub Actions use the Node 24 runtime.
- `henchmen serve` now enters every mounted service's lifespan, so the Slack
  bot connects and `/mastermind/metrics/*` works under `serve` and Docker
  Compose. The three services share one document store.
- The Mastermind lint gate and `fix_lint` judge and fix only files the
  operative changed (Python via ruff, JS/TS via eslint from the nearest
  `package.json`, Go via `go vet`), and fail closed if the diff against the
  base branch cannot be computed. `fix_lint` no longer runs ruff on non-Python
  stacks and reverts any auto-fix outside the operative's changes.
- Forge reports a CI run as `incomplete`, not passed, when a check cannot run,
  and caps a run at `forge_ci_timeout_seconds` (540s) so it finishes inside
  Pub/Sub's ack deadline. The Forge Cloud Run service timeout is 600s.
- The embedding pipeline moved from Dispatch to `dossier/embed_pipeline.py`.
  A full re-index clears the repository's existing chunks first, and re-uploading
  a changed file replaces its old chunks instead of duplicating them.
- Bedrock tier defaults are cross-region inference profiles
  (`us.anthropic.claude-sonnet-4-20250514-v1:0`,
  `us.anthropic.claude-haiku-4-5-20251001-v1:0`).
- The local SQLite store defaults to `~/.henchmen/henchmen_<environment>.db`
  instead of a working-directory-relative file.
- Scheme registration rejects an agentic node whose `model_name` is not a model
  tier.
- The stalled-task watchdog and the dead-letter check return 503 when their
  query fails, instead of reporting zero results.
- Terraform creates 7 Pub/Sub topics; `task-planned`, `operative-dispatch` and
  `operative-status` had no publisher and are gone.
- Vertex AI applies `vertex_ai_safety_threshold` to every call and routes
  `gemini-3*` models to the global endpoint. OpenAI and Bedrock replace another
  vendor's model name with their complex-tier model instead of returning 404.
- PyGithub is pinned `>=2.4.0,<3` and authenticates with `Auth.Token`.
- Local-mode lint/test gates no longer bind-mount a host workspace: a gate container from the operative
  image clones the branch, computes the diff against `origin/<base>` and runs the scoped commands itself.
- `PUT /console/api/setup/state` no longer accepts `completed_steps`; only a step's validation route marks
  it complete.

### Fixed
- **Tier names reached provider APIs unresolved.** Vertex AI, OpenAI and
  Bedrock were sent the literal string `default/complex` as a model id, so
  every agentic node failed on those providers.
- **Circular imports** made `henchmen.dossier`, `henchmen.models.dossier` and
  `henchmen.mastermind.server` unimportable in a fresh interpreter — the
  Mastermind container crashed on start.
- **The local CI gate could not fail.** A command suffixed with
  `2>/dev/null || echo SKIP` always exited 0, so lint and test failures were
  reported as passes.
- **`create_pr` fabricated a PR URL** and returned `condition: "pass"` when the
  GitHub token was missing, finalising the task and triggering CI on a PR that
  did not exist. It now fails closed, as do unknown deterministic nodes and
  undetectable project stacks.
- **`CloudRunOrchestrator` dropped the `secrets` argument**, so `GITHUB_TOKEN`
  never reached an operative on GCP, and `get_status` compared against
  condition names that do not exist, so it could only ever report
  `PROVISIONING`.
- **A local operative published its report to its own in-process broker**, so
  Mastermind never received it. The broker now forwards to the host when
  `local_forward_base_url` is set.
- Cost was priced at Anthropic rates for every provider once schemes used tier
  names, over-counting Gemini by up to 9x and tripping the ceiling early.
- `symbol_lookup` relied on a GNU word-boundary extension and silently returned
  zero matches on grep builds that ignore it in `-E` mode.
- The GitHub PR-comment trigger had no authorization: any commenter could start
  a paid operative run. It now requires a trusted `author_association`.
- Terraform: Mastermind's `run.developer` role was conditioned to services, not
  jobs, so every lair provisioning was denied; it had no Firestore role; and
  neither `HENCHMEN_FIRESTORE_DATABASE` nor `HENCHMEN_PUBSUB_OIDC_AUDIENCE` was
  injected, so services opened the wrong database and rejected every Pub/Sub
  push with 401.
- The Slack bot published from a Bolt worker thread via
  `asyncio.get_event_loop()`, which raises on Python 3.12+; fetched thread
  context was dropped by the normalizer; and mention stripping matched a
  literal `<@henchmen>` that Slack never sends.
- Jira webhook signatures are verified against `X-Hub-Signature`, the header
  Jira Cloud actually sends.
- `henchmen doctor` ignored `.env.local` entirely because it read `os.environ`.
- Integration tests no longer authenticate with the developer's real
  credentials; `integration_settings` blanks them.
- The operative wrote `last_heartbeat` as a datetime while the tracker and the
  stalled-task query use ISO strings, so a task whose operative had heartbeated
  once could never be detected as stalled.
- `fix_lint` did not await or check its git steps and could report a push that
  never happened; interrupted operatives' saved reports were ignored in favour
  of a fabricated failure.
- Task intake swallowed every exception, so Pub/Sub's retry path was
  unreachable.
- The embedding pipeline advanced the last-indexed commit after a partial
  upload, permanently stranding the chunks that failed.
- `henchmen chat` sent a `type` field the task endpoint rejects, so every task
  dispatched from chat failed with 422.
- An eval fixture requiring tests but declaring no test command could score 1.0.
- The operative ran on without a document store outside dev, silently losing
  heartbeats and the cost ceiling; it now fails the job.
- The Slack Socket Mode bot published redelivered events as new tasks.
- `symbol_lookup`-style scoping: `test_runner` fails closed on unknown project
  types; `git_branch_create` branches from the repository's real default branch.
- DynamoDB reads returned `Decimal` for numbers nested in maps and lists.

### Removed
- `src/henchmen/arsenal/server.py` (the FastMCP tool server) and the `mcp`
  dependency — Arsenal runs in-process inside the operative.
- `src/henchmen/forge/ci_orchestrator.py` and `pr_builder.py` — dead code that
  nothing imported; Forge runs CI in `server.py` and Mastermind opens PRs.
- The `henchmen_dev.db` SQLite database and its WAL/SHM files are no longer
  tracked in git.
- Settings nothing read: `arsenal_mcp_server_url`, `gcs_bucket_tfstate`,
  `github_app_id`, `github_app_private_key_secret`, `pubsub_topic_task_planned`,
  `pubsub_topic_operative_dispatch`, `pubsub_topic_operative_status`,
  `vertex_ai_grounding_enabled`, `vertex_ai_context_cache_enabled`,
  `vertex_ai_context_cache_min_tokens`.
- `SchemeNode.grounding_enabled`; `fix_tests` no longer requests Google Search
  grounding.
- The `/pubsub/task-planned` handler, the operative's snapshot-cache lookup
  (it could never hit), `google-cloud-logging` from the Forge and operative
  images, and git from the Dispatch image.

### Security
- Desktop gate containers (local CI gates, `fix_lint`, Forge CI) receive the GitHub token only on stdin,
  never in argv or an environment. They run every repository-controlled command as uid 65534, with a
  minimal capability set, `no-new-privileges` and `--init`. `fix_lint` pushes from a root-only git
  directory with hooks disabled.
- An operative report or interrupted report body over the size cap is answered 413, and the operative
  treats that as undeliverable. Operatives cap `git_diff` at 512 KiB. Task-token reports are accepted
  only from the lair Mastermind launched for that task and node.
- The needs-attention Console returns its problem list only to a signed-in session. An unauthenticated
  `/console/api/status` still reports the mode.
- `EnvFile` (and so `henchmen init` and the Console) and every secret file refuse a symlinked,
  non-regular or foreign-owned file with an actionable error rather than following it.
- Secret redaction now applies to the whole `henchmen` logger tree and formats
  the record before matching, so `%s` arguments are redacted too. Anthropic
  `sk-ant-` keys were added to the pattern.
- `/metrics` requires a bearer token when `metrics_auth_token` is set, and
  returns 401 in staging and prod when it is not. It no longer returns raw task
  payloads.
- `git_push` and `git_force_push` parse `src:dst` refspecs, so a push to a
  protected branch cannot be disguised; force-push additionally requires
  `allow_force_push`.
- Arsenal subprocesses have timeouts and decode with replacement characters
  rather than raising on invalid bytes.
- `.gitleaks.toml` loads the default rule set; the hook was scanning with zero
  rules.
- Terraform's seeded placeholder secret is treated as unset for every
  credential, so the metrics bearer token, Slack signing secret and webhook
  secrets no longer accept a value published in this repository.
- `POST /api/v1/tasks` requires `Authorization: Bearer <HENCHMEN_DISPATCH_API_TOKEN>`
  (open only in dev with a warning; 401 in staging and prod).
- `/api/v1/metrics/summary` requires the metrics bearer token.
- Secret redaction is installed in Dispatch, Forge and the Slack bot, which
  previously logged tokens unredacted.

## [0.2.1] - 2026-04-12

Direct-LLM providers and the interactive task builder.

### Added
- `henchmen chat` — an interactive REPL that turns a conversation into a
  structured task and dispatches it.
- A separate chat model setting so the task builder need not use the operative
  model.

### Fixed
- Anthropic provider: tool and assistant message conversion, orphaned
  `tool_result` cleanup, tier resolution inside `generate()`, and corrected
  model ids.
- Local mode: CI checks run inside Docker, `github_token` reads were unified,
  scheme selection priority and datetime serialization were corrected, and a
  task branch missing on the remote falls back to main.

## [0.1.1] - 2026-04-10

The OSS-readiness release — 7.5/10 → 10/10. Closes every deferred TODO
from the 2026-04-09 expert-panel remediation, tightens the supply-chain
story, and ships a reproducible path from clone to running stack. See
[`docs/releases/2026-04-10-v0.1.1.md`](docs/releases/2026-04-10-v0.1.1.md)
for the narrative post.

### Added
- `docs/deploy-gcp.md` — 30-minute self-hoster walkthrough from blank GCP account to live stack
- `henchmen doctor` CLI command: self-check for Docker, git identity, LLM credentials, operative image, `.env.local`, Python version
- `src/henchmen/utils/stack_detector.py` + `tests/unit/test_stack_detector.py` — language stack detection (Python, Node pnpm/npm, Go, Rust, Java Maven/Gradle) used by the `run_tests` scheme handler
- 3 new eval fixtures: `bugfix_import_error`, `feature_cli_flag`, `refactor_extract_function`
- `.github/workflows/evals.yml` — `workflow_dispatch` workflow that runs the eval harness for one provider and opens a PR with the updated `evals/baseline.json`
- `evals/baseline.json` is now a structured stub with per-provider `how_to_populate` commands
- `DocumentStore.increment(collection, doc_id, field_deltas)` — atomic counter primitive across GCP Firestore, SQLite, and DynamoDB
- `DocumentStore.update_if(collection, doc_id, expected_field, expected_value, new_values)` — compare-and-set primitive across all three providers
- `HENCHMEN_LLM_OLLAMA_SKIP_PROBE` setting + Ollama up-front tool-calling capability probe (raises clear error for non-capable models instead of silently falling back)
- `.github/workflows/ci.yml` now has a `docker-compose-smoke` job that runs `docker compose up -d`, waits for healthy status, and tears down
- `tests/integration/test_reliability_guards.py` — integration test for cost-ceiling breakers and silent-failure detection
- `pytest-randomly` in the `[dev]` extras; unit suite verified green across seeds 42 / 1234 / 9999
- `docs/releases/2026-04-10-v0.1.1.md` — narrative release post covering motivation for every change
- `docs/images/metrics-sample.txt` — sample `/metrics/prometheus` output with regeneration instructions
- "Verified today" section in README linking CI badge, expert review, deploy-gcp walkthrough, evals workflow, supply-chain pins, and `henchmen doctor`
- "Supported Languages" and "Reproducibility" sections in README

### Changed
- All four Dockerfiles (`containers/{dispatch,forge,mastermind,operative}/Dockerfile`) now pin base images to real sha256 digests (`python:3.12.8-slim-bookworm@sha256:2199a6...`, `node:20.18-slim@sha256:ffc11d...`). Removed the `TODO: pin to actual digest before first release` comments.
- `terraform/modules/data-stores` now deploys `firestore.rules` via `google_firebaserules_ruleset` + `google_firebaserules_release` resources. `firestore.rules` is now a `templatefile()` with the mastermind SA email regex interpolated from `var.project_id`. Previously the rules file was a stub not deployed by Terraform.
- `terraform/modules/project-bootstrap` enables `firebaserules.googleapis.com` alongside the other required APIs.
- `MergeQueue.dequeue` now uses `DocumentStore.update_if` as an atomic claim instead of a best-effort FIFO read-modify-write. Removed all `TODO(E7-transaction)` markers from `src/henchmen/forge/merge_queue.py`.
- `TaskTracker.record_node_result`, `increment_recovery_attempts`, and `record_ci_fix_attempt` now use `DocumentStore.increment` instead of the read-modify-write block. Removed all `TODO(K4-cross-process)` markers from `src/henchmen/observability/tracker.py`.
- `src/henchmen/providers/aws/sns.py::pull_dlq` now has a full SQS implementation (lazy boto3 client, `get_queue_url` + `receive_message` + `delete_message_batch`). Previously raised `NotImplementedError`.
- `run_tests` scheme handler in `src/henchmen/mastermind/scheme_executor/handlers.py` now routes via `stack_detector.detect_stack()` instead of assuming pnpm+turbo. JS/TS monorepo handling is preserved as a legacy branch.
- `src/henchmen/dispatch/slack_bot.py` — converted 6 remaining `print()` calls to `logger.*`. Only `structured_logging.py` still uses `print()`, and the reason is now documented in the module docstring.
- `README.md` provider matrix marks AWS as **experimental / community-contributed** with a GitHub Discussions link. GCP and Local are the only **supported** providers.
- `docs/operations.md` and `docs/rollback-procedures.md` rewritten in self-hoster voice — removed internal Slack / on-call / project-name references and pointed setup questions at `docs/deploy-gcp.md`.
- All 8 per-file `_mock_settings()` helpers in the test suite now build a real `Settings` instance via `model_copy(update=...)` instead of a `MagicMock`. Catches schema drift.

### Fixed
- All 56 `pytest.mark.skip` integration tests un-quarantined. `tests/integration/` now reports **144 passed, 0 skipped**:
  - `test_forge_pipeline.py` — refactored to inject mock `MessageBroker` + `DocumentStore` (18 tests)
  - `test_end_to_end.py` — stub `SchemeExecutor.execute`, add `files_changed` (13 tests)
  - `test_mastermind_orchestration.py` — deleted `TestStateMachineIntegration`, parametrized scheme selection, updated handler patches (20 tests)
  - `test_dispatch_pipeline.py::TestDispatchNormalizerIntegration` — inject mock broker via `publish_task(..., broker=)` (6 tests)
- Pre-existing Terraform syntax error in `cloud-run-services/main.tf` (single-line nested blocks no longer supported in `terraform >= 1.7`) fixed.

### Removed
- No references to any specific target repository remain in the public repo. Fixture data uses `acme-org/sample-repo`.
- `TODO(K4-cross-process)` and `TODO(E7-transaction)` markers deleted from `src/henchmen/observability/tracker.py` and `src/henchmen/forge/merge_queue.py`.
- `TODO: pin to actual digest before first release` comments removed from all 4 Dockerfiles.

### Security
- Supply-chain: base image digest pinning closes the "unpinned upstream" findings from the 2026-04-09 review. Every CI run and every release now builds from a known-good image digest.
- Firestore rules are now deployed via Terraform — previously they were a stub file that would have had to be deployed manually via `firebase deploy`. Collection-level authorization is now infrastructure-as-code.

## [0.1.0-rc1] - 2026-04-09

The 2026-04-09 expert-panel remediation pass. Groups of changes below are
labelled with finding IDs from the internal audit (`A*`, `K*`, `S*`) so the
trail from "finding filed" to "finding closed" is auditable.

### Added

Security

- GitHub webhook signature verification is now always on and fail-closed in
  staging and prod. Dev mode retains the same behaviour behind an explicit
  opt-out. (A1)
- Slack signing secret validation on all intake paths, with a hard
  failure when the secret is missing in non-dev environments. (A2)
- Pub/Sub push subscriptions now require an explicit OIDC audience via
  `HENCHMEN_PUBSUB_OIDC_AUDIENCE`. Missing audience raises at startup instead
  of producing silent 403s. (A3) *Correction: a missing audience is reported by
  `validate_for_runtime` and rejects pushes in staging and prod; it does not
  raise at startup.*
- Per-task LLM cost ceiling (`HENCHMEN_COST_CEILING_USD_PER_TASK`, now
  `HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD`) with a
  fail-closed breaker when a task exceeds the limit. (A4)
- Secret redaction on all structured log records for known token shapes
  (GitHub, Slack, OpenAI, Anthropic). (A5)
- `SECURITY.md` now documents the threat model, out-of-scope items, supported
  versions, safe harbour for good-faith researchers, and response SLOs. (A8)

Reliability

- `/metrics/prometheus` endpoint exposing `henchmen_tasks_completed_total`,
  `henchmen_tasks_escalated_total`, `henchmen_ci_pass_rate` (unset when no
  data), and `henchmen_cost_usd_total`. Returns a helpful 503 when the
  `observability` extras are not installed. (K8)
- Healthchecks on all docker-compose services with `depends_on:
  condition: service_healthy` so containers wait for real readiness. (S9)
- Stuck-task watchdog endpoint (`/api/v1/watchdog`) callable from a local
  cron for self-hosted deployments that lack Cloud Scheduler. (K9)

Developer Experience

- `docs/troubleshooting.md` covering the 15 most common self-hosted failure
  modes (operative image missing, Slack misconfig, Ollama empty responses,
  Forge CI hangs, cost ceiling, Pub/Sub OIDC, force-push gate, CRLF in
  `.env.local`, and more). (S8)
- Self-hosted operations mapping at the top of `docs/incident-runbook.md`,
  `docs/operations.md`, and `docs/rollback-procedures.md` so users without
  `gcloud` know which sections apply to them. (K9)
- `CONTRIBUTING.md`-style header on `CLAUDE.md` clarifying it is for AI
  assistants, and pointing humans at `CONTRIBUTING.md` first. (S12)

### Changed

Security

- GitHub webhook handler no longer defaults to fail-open when the signing
  secret is unset in staging / prod. Dev behaviour is unchanged and gated on
  `HENCHMEN_ENVIRONMENT=dev`. (A1)
- Secret Manager is now the only supported secret source in GCP
  environments; `.env.local` support is restricted to `HENCHMEN_PROVIDER=local`. (A5)
  *Correction: `.env.local` is loaded for every provider.*

Reliability

- `/metrics/summary` now returns `null` (JSON `null`) for `ci_pass_rate`
  when there is no decided CI data, rather than `0.0`. This prevents
  self-hosters from being paged by alerts like `ci_pass_rate < 0.5` on empty
  time windows. (K8)
- Per-scheme `ci_pass_rate` in the summary response follows the same rule. (K8)
- `docker-compose.yml` normalises all three services to consistent
  `command:` declarations and `depends_on` blocks, and pins
  `ollama/ollama:0.4.6` instead of tracking `latest`. (S9)

Documentation

- `README.md` "Documentation" section no longer links to the internal
  `docs/operations.md`; it now points to the new
  `docs/troubleshooting.md`. (S11)
- `CHANGELOG.md` committed to the Keep a Changelog format with an
  explicit semver policy for 0.x. (S7)

### Fixed

Security

- Webhook verifier no longer logs the rejected signature at INFO level,
  which previously leaked partial secret material. (A1)
- Removed a timing-safe comparison bypass in the Slack signature
  verification path that could return early on mismatched lengths. (A2)

Reliability

- `TIMED_OUT` operatives are no longer upgraded to `COMPLETED` in the
  mastermind state machine -- timing out now stays terminal, matching the
  documented invariant. (K1)
- CI failure auto-fix loop no longer double-dispatches under concurrent
  Pub/Sub delivery; the `ci_fix_in_progress` flag is now set transactionally. (K2)
- Lair provisioning failure in staging / prod is now correctly fail-closed
  (previously it could return `condition: "pass"` on certain exception
  paths). (K3)
- Operative cost tracker recognises `gemini-2.5-flash` and `gemini-3.1-pro`
  in the price map; previously these reported `$0.00`. (K4)
- Metrics API no longer crashes when a task record is missing the
  `scheme_id` field. (K5)

Developer Experience

- `pip install -e ".[dev]"` no longer pulls GCP SDKs. Use `dev-integration`
  extras when running integration tests. (S1)
- `ruff` configuration pinned to the rule set documented in `CLAUDE.md`. (S2)
- Windows CRLF handling in `.env.local` is now documented and does not
  produce subtly-wrong secrets at runtime. (S3)

### Security

- Webhook fail-open in non-dev is fixed as described above. (A1)
- Slack signing secret is now mandatory in staging / prod. (A2)
- Pub/Sub OIDC audience is now mandatory in staging / prod. (A3)
- Per-task LLM cost ceiling prevents runaway spend from a compromised
  upstream. (A4)
- Log redaction for token-shaped secrets in structured logs. (A5)
- `SECURITY.md` now has an explicit threat model, reporting instructions,
  and safe harbour. (A8)

### Removed

- Legacy `HENCHMEN_DEV_MODE` boolean that shadowed `HENCHMEN_ENVIRONMENT`.
  Use `HENCHMEN_ENVIRONMENT=dev` instead. (S4)
- Dead code paths for the old single-provider HTTP CI hook, superseded
  by the `CIProvider` abstraction. (S5)

## [0.1.0] - 2026-04-08

### Added
- Initial open source release
- Provider interface layer with 6 abstractions (MessageBroker, DocumentStore, ObjectStore, ContainerOrchestrator, LLMProvider, CIProvider)
- GCP providers (Pub/Sub, Firestore, GCS, Cloud Run, Vertex AI Gemini, Cloud Build)
- AWS providers (SNS, DynamoDB, S3, ECS Fargate, Bedrock, CodeBuild)
- Local providers (in-memory, SQLite, filesystem, Docker, Ollama, shell CI)
- OpenAI and Anthropic direct API LLM providers
- Docker Compose local development stack with Ollama
- `henchmen serve` single-process CLI command
- 7 villain-themed components: Mastermind, Dispatch, Operative, Arsenal, Forge, Dossier, Schemes
- Apache 2.0 license
