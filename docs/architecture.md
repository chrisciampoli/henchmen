# System Architecture

Henchmen Agent Factory is a production AI agent system that dispatches coding operatives to fix bugs and implement features in target repositories. It is inspired by Stripe's Minions architecture: a central orchestrator selects a workflow (Scheme), walks a DAG of deterministic and agentic nodes, provisions ephemeral containers (Lairs) for each agentic node, and opens a pull request with the results.

This page describes the GCP deployment. In local mode (`henchmen serve`) the same three services run in one process, Pub/Sub is an in-memory broker, Firestore is SQLite and each Lair is a Docker container.

## High-Level Architecture

```
                        Slack / GitHub / Jira / CLI
                                  |
                          +-------v--------+
                          |    Dispatch    |  Cloud Run Service
                          | (Task Intake)  |  Normalizes events into HenchmenTask
                          +-------+--------+
                                  |  Pub/Sub: task-intake
                                  v
                          +-------+--------+
                          |   Mastermind   |  Cloud Run Service
                          | (Orchestrator) |  Selects scheme, builds dossier,
                          +--+----------+--+  walks DAG, runs lint/test gates,
                             |          |     opens the PR
               +-------------+          +-------------+
               |                                      |
     +---------v---------+                  +---------v---------+
     |    Lair (CRJ)     |                  |    Lair (CRJ)     |
     | implement_fix     |                  | fix_tests         |
     | tier default/     |                  | tier default/     |
     |   complex         |                  |   reasoning       |
     +---------+---------+                  +---------+---------+
               |                                      |
               |  Pub/Sub: operative-complete         |
               +------------------+-------------------+
                                  |
                                  v
                            Mastermind
                                  |  create_pr, then Pub/Sub: forge-request
                                  v
                          +-------+--------+
                          |     Forge      |  Cloud Run Service
                          | (CI Pipeline)  |  Lint, test, silent-failure scan,
                          +-------+--------+  PR comment
                                  |  Pub/Sub: forge-result
                                  v
                            Pull Request
```

CRJ = Cloud Run Job. The model tier each Lair runs on is resolved to a concrete model by the configured LLM provider (see [Model Routing](#model-routing)).

### Component Summary

| Component | Runtime | Purpose |
|-----------|---------|---------|
| **Dispatch** | Cloud Run Service | Ingests tasks from Slack, GitHub, Jira, or CLI. Normalizes into `HenchmenTask` and publishes to Pub/Sub. No business logic. |
| **Mastermind** | Cloud Run Service | Central orchestrator. Selects a Scheme, builds a Dossier, walks the DAG, provisions Lairs for agentic nodes, runs the deterministic lint/test gates, opens PRs. |
| **Operative** | Cloud Run Job (Lair) | Ephemeral agent container. Clones repo, runs an agentic tool loop against an LLM, commits and pushes changes. |
| **Arsenal** | In-process registry (inside the Operative) | Tool system providing the `code_edit`, `code_intel`, `context`, `git_ops`, `github`, `jira`, `slack` and `test_runner` tool sets. |
| **Forge** | Cloud Run Service | Post-PR CI pipeline. Clones the PR branch, runs lint/tests/silent-failure detection, comments on the PR, publishes `forge-result`. Never opens or merges PRs. |
| **Dossier** | Library (in Mastermind) | Context assembly. Fetches file trees, rule files, related PRs/issues, semantic code chunks from Vertex AI RAG Engine (corpus: `henchmen-code`). |
| **Tracker** | Library (in Mastermind) | Observability layer. Persists per-task and per-node telemetry (tokens, cost, duration) to the document store. |

## Detailed Component Architecture

### Dispatch

**Source:** `src/henchmen/dispatch/server.py`, `normalizer.py`, `handlers/`, `slack_bot.py`
**Container:** `containers/dispatch/Dockerfile`
**Cloud Run service:** `henchmen-{env}-dispatch`

Dispatch is the system's front door:

- `POST /api/v1/tasks` -- CLI/REST task creation (`CreateTaskRequest`: `title`, `description`, `repo`, `branch`, `priority`, optional `task_type`, `created_by`). Requires `Authorization: Bearer <HENCHMEN_DISPATCH_API_TOKEN>`; with the token empty the route is open in dev (one logged warning) and returns 401 in staging and prod.
- `POST /webhooks/slack` -- Slack Events API (handles `url_verification` and `app_mention`). Slack is normally connected over **Socket Mode** instead, started from the Dispatch lifespan when `HENCHMEN_SLACK_BOT_TOKEN` and `HENCHMEN_SLACK_APP_TOKEN` are both set.
- `POST /webhooks/github` -- Repository webhook signed with `HENCHMEN_GITHUB_WEBHOOK_SECRET` (issues labelled `henchmen`, `@henchmen` comments from trusted users, CI failures, pushes)
- `POST /webhooks/jira` -- Jira webhook signed with `HENCHMEN_JIRA_WEBHOOK_SECRET`
- `POST /pubsub/task-planned` -- Legacy push endpoint that only logs; Terraform no longer creates a `task-planned` topic, so nothing calls it

Each handler uses the `TaskNormalizer` to convert the source-specific payload into a `HenchmenTask` Pydantic model, then publishes the serialized task to the `henchmen-{env}-task-intake` topic. GitHub CI-failure events go to `ci-failure`; push events go to `embed-request`.

### Mastermind

**Source:** `src/henchmen/mastermind/agent.py`, `scheme_executor/executor.py`, `scheme_executor/handlers.py`, `lair_manager.py`, `server.py`
**Container:** `containers/mastermind/Dockerfile`
**Cloud Run service:** `henchmen-{env}-mastermind`

The Mastermind is the brain of the system. It receives tasks via Pub/Sub push subscription and orchestrates the full execution lifecycle:

1. **Scheme Selection** (`_select_scheme`): Goal keywords in the title route to `goal_decomposition` first. Otherwise an explicit `task_type` on the task wins (`bugfix` runs `bugfix_standard`; `feature` and `refactor` run `feature_standard`). Without one, keyword matching on word boundaries sends bug keywords to `bugfix_standard`, then feature keywords to `feature_standard`; anything else defaults to `bugfix_standard`.

2. **Dossier Building** (`_build_dossier`): Assembles context for operatives:
   - Fetches the repository file tree from GitHub and keeps the first 50 paths for file scoring
   - Runs `TaskAnalyzer` to extract mentioned files, error patterns, and keywords
   - Fetches CI failure data if the task is CI-related
   - Pre-fetches file contents for explicitly mentioned files
   - Queries Vertex AI RAG Engine (corpus: `henchmen-code`) for the top 20 semantically relevant code chunks
   - Fetches repo rule files (CLAUDE.md, etc.) and related PRs via `DossierBuilder`

3. **Scheme Execution** (`SchemeExecutor`): Walks the scheme DAG from root to terminal node:
   - **Deterministic nodes** run inline handlers: `create_branch`, `prefetch_context`, `verify_changes`, `run_lint`, `fix_lint`, `run_lint_retry`, `run_tests`, `run_tests_retry`, `create_pr`, `escalate`, `report_plan`. A deterministic node with no registered handler fails.
   - **Agentic nodes** (`implement_fix`, `implement_feature`, `fix_tests`, `analyze_goal`) are dispatched to Lairs via `LairManager`
   - Edge conditions (`pass`/`fail`) determine the next node. Unconditional edges are followed as fallback.
   - A per-node execution limit (2) forces `fail` if a node would run a third time.
   - A per-task cost ceiling (`HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD`) is checked before each agentic node is dispatched.

4. **Task Lifecycle Tracking**: There is no separate state-machine class. A task carries a `TaskStatus` (`pending`, `dispatched`, `in_progress`, `completed`, `failed`, `escalated`), and its `task_executions` document records `final_status`, `execution_state` (`running`, `completed`, `escalated`, `stalled`) and a `last_heartbeat`. The watchdog (`POST /api/v1/watchdog`, every 5 minutes from Cloud Scheduler; dev sets `scheduler_enabled = false`, so call it by hand there) re-publishes tasks whose heartbeat expired and escalates after 3 recovery attempts.

5. **CI Failure Loop** (`handle_ci_failure`): When CI fails on a Henchmen PR, the Mastermind can dispatch a fix operative (max 2 attempts). It extracts errors from GitHub check run annotations and dispatches a new Lair with the error context.

**HTTP endpoints on Mastermind:**
- `POST /pubsub/task-intake` -- Receive new tasks (processes asynchronously)
- `POST /pubsub/operative-complete` -- Receive operative completion reports
- `POST /pubsub/forge-result` -- Receive CI results from Forge
- `POST /pubsub/ci-failure` -- Receive CI failure notifications for auto-fix
- `POST /api/v1/watchdog`, `/api/v1/check-dlq`, `/api/v1/cleanup` -- Cloud Scheduler jobs
- `GET /metrics/summary`, `/metrics/tasks`, `/metrics/tasks/{task_id}`, `/metrics/prometheus` -- Metrics API (see `docs/operations.md`)

### Operative (Lair)

**Source:** `src/henchmen/operative/bootstrap.py`, `agent_builder.py`, `guardrails.py`, `prompt_templates.py`
**Container:** `containers/operative/Dockerfile`
**Cloud Run Job:** `lair-{task_id[:8]}-{node_id}-{suffix}`, created per agentic node by `LairManager`

Each operative runs as an ephemeral Cloud Run Job. Its `OperativeStatus` moves through:

```
spawning -> initializing -> executing -> reporting -> completed | failed | timed_out | blocked | interrupted
```

`timed_out` is never upgraded to `completed`.

**Initializing:** Clones the target repository, checks out the feature branch (`henchmen/{task_id[:8]}`), installs project dependencies (npm/pnpm for Node.js), downloads dossier context from the object store, and pre-reads the most relevant files (up to 5, 4000 characters each) into context.

**Executing:** Builds an `OperativeAgent` wired with tools from the Arsenal registry. The agent runs an agentic loop:
1. Send conversation (system prompt + task + dossier context) to the LLM
2. LLM returns tool calls or text
3. Execute tool calls via Arsenal handlers (tool results over 10,000 characters are truncated)
4. Append results and repeat until `git_commit` succeeds or the step budget is exhausted

The agent includes:
- **Phase-aware nudging:** If the model spends too many steps only reading files, it receives an escalating prompt to start editing.
- **Commit detection:** The loop breaks immediately after a successful `git_commit` call.
- **Timeout management:** Agent timeout is `node.timeout_seconds - 120s` (buffer for branch push), with a 60-second floor.

**Reporting:** After execution, the operative checks for changes (uncommitted or committed-ahead-of-base), pushes the branch, then publishes an `OperativeReport` to the `operative-complete` topic.

**Guardrails** (`OperativeGuardrails`):
- Blocks disallowed tools (only tools matching the node's `ArsenalRequirement` are permitted)
- Blocks path traversal attempts (`../`, `..\\`)
- Truncates oversized messages (>64K chars)
- Enforces step limits, the per-task cost ceiling and the wall-clock ceiling (`HENCHMEN_OPERATIVE_WALLCLOCK_CEILING_SECONDS`)
- Tracks token usage and tool call telemetry

### Arsenal

**Source:** `src/henchmen/arsenal/registry.py`, `src/henchmen/arsenal/tools/`

Arsenal is the tool system. It runs in-process inside the Operative -- it is not a separate service. A decorator-based registry (`@tool`) registers functions as callable tools, organized into eight categories:

| Category | Tools | Description |
|----------|-------|-------------|
| `code_intel` | `file_read`, `file_search`, `symbol_lookup`, `grep_search`, `ast_analysis` | Read-only code exploration |
| `code_edit` | `file_write`, `file_edit`, `file_create`, `file_insert_at_line`, `file_delete` | File modification (`file_delete` is destructive) |
| `context` | `semantic_search`, `find_related` | Semantic search over the RAG corpus |
| `git_ops` | `git_branch_create`, `git_commit`, `git_push`, `git_force_push`, `git_diff`, `git_log`, `git_status` | Git operations (`git_force_push` is destructive and also requires `HENCHMEN_ALLOW_FORCE_PUSH`) |
| `github` | `create_pull_request`, `comment_on_pr`, `label_issue`, `assign_issue`, `fetch_issues` | GitHub API |
| `jira` | `update_issue_status`, `add_comment`, `transition_issue`, `fetch_issue` | Jira API |
| `slack` | `post_message`, `thread_reply`, `upload_file` | Slack API |
| `test_runner` | `run_tests`, `run_lint`, `type_check` | Test/lint/type-check execution (auto-detects Python vs Node.js) |

Each `SchemeNode` declares an `ArsenalRequirement` naming which tool sets the operative can access and whether destructive operations are allowed. The allowed tool-set names are the `ArsenalToolSet` literal in `src/henchmen/models/scheme.py`, so a scheme cannot request a category nothing registers. `ToolRegistry.get_tools_for_requirement()` filters accordingly.

Notable tool features:
- `file_edit` supports fuzzy whitespace matching and Unicode normalization (handles LLM-generated smart quotes, em dashes)
- `run_lint` and `run_tests` auto-detect project type (Python uses ruff/pytest/mypy; Node.js uses eslint/jest/tsc)
- Relative paths in tool arguments are automatically resolved to the workspace directory

### Forge

**Source:** `src/henchmen/forge/server.py`, `ci_runner.py`, `silent_failure_detector.py`, `merge_queue.py`, `error_extractor.py`
**Container:** `containers/forge/Dockerfile`
**Cloud Run service:** `henchmen-{env}-forge`

The Forge handles post-PR CI validation. It does not open PRs -- the Mastermind's `create_pr` node does, then publishes `forge-request`.

1. **CI Runner** (`CIRunner`): Clones the PR branch, runs `ruff check` on the Python files the PR changed, runs the tests (`pytest`, or `npm test` for a Node target), and runs the silent failure scan on the PR diff. Lint and the scan fail closed when the PR base cannot be resolved. The result is `passed` only when every check ran and passed; a check that could not run (for example, the target's tests need a tool the Forge image lacks) makes the run `incomplete`, which the PR comment flags and Mastermind does not treat as a pass.

2. **Silent Failure Detector** (`SilentFailureDetector`): Scans the git diff for patterns that indicate silent failures:
   - `critical`: Empty catch blocks, bare `except: pass`, hardcoded secrets
   - `warning`: Catch-return-null, catch without logging, retry without backoff, noop changes
   - `info`: TODO/FIXME comments
   - Only critical findings cause the CI check to fail.

3. **Merge Queue** (`MergeQueue`): A claim queue in the `merge_queue` collection (`pending -> merging -> merged | failed`). Nothing in Henchmen enqueues into it today — every PR is merged by a human — so the periodic tick (`/api/v1/process-queue`) only expires `merging` claims older than their TTL and reports the queue depth.

4. **Error Extractor** (`error_extractor.py`): Fetches GitHub check run annotations for failed CI suites and formats them as structured context for fix operatives.

**HTTP endpoints on Forge:**
- `POST /pubsub/forge-request` -- Receive CI run requests (clones branch, runs checks, comments on PR, publishes `forge-result`)
- `POST /pubsub/build-complete` -- Cloud Build completion callback
- `POST /api/v1/process-queue` -- Merge queue maintenance tick (Cloud Scheduler): expires stale claims, reports depth

### Dossier

**Source:** `src/henchmen/dossier/builder.py`, `rules.py`, `cache.py`, `task_analyzer.py`, `chunker.py`, `embedder.py`, `convention_detector.py`, `file_scorer.py`, `reranker.py`

The Dossier subsystem assembles context packages for operatives:

- **DossierBuilder** (`builder.py`): Orchestrates fetching relevant files, rule files (CLAUDE.md, .cursorrules, etc.), related PRs, related issues, and code search results, and runs convention detection. Uploads the assembled dossier as JSON to the object store.
- **Rules** (`rules.py`): Finds and loads repository rule files.
- **TaskAnalyzer** (`task_analyzer.py`): Classifies tasks by type (bug_fix, test_fix, feature, refactor, generic), extracts mentioned files, error patterns, and keywords using regex patterns.
- **ConventionDetector** (`convention_detector.py`): Detects naming, indentation, test-framework and lint conventions from config files and sampled sources so generated code matches the project's style.
- **FileScorer** (`file_scorer.py`): Scores files for relevance from weighted signals (task mentions, RAG hits, directory proximity, recent changes, stack traces).
- **Chunker/Embedder** (`chunker.py`, `embedder.py`): Indexes repository code into Vertex AI RAG Engine (corpus: `henchmen-code`) for semantic search.
- **Reranker** (`reranker.py`): Reranks RAG chunks with the light-tier model; not yet called from the Mastermind pipeline.
- **SnapshotCache** (`cache.py`): Helpers for storing repository snapshots in the snapshots bucket. Nothing creates snapshots, so operatives always clone the repository fresh.

## Data Flow

### Task Lifecycle

```
1. User @mentions the bot in Slack: "fix the login bug"
   |
2. Dispatch receives the Slack event, normalizes to HenchmenTask
   |
3. HenchmenTask published to Pub/Sub: task-intake
   |
4. Mastermind receives task via push subscription
   |
5. Scheme selection: "bugfix_standard" (keyword: "fix")
   |
6. Dossier building: file tree, task analysis, RAG chunks, rule files
   |
7. SchemeExecutor walks the DAG:
   |
   create_branch -> prefetch_context -> implement_fix (AGENTIC, tier default/complex)
                                               |
                                         [Lair provisioned]
                                         [Operative reads code, makes fix, commits]
                                               |
                                         verify_changes (DETERMINISTIC: source commits ahead of base?)
                                               |
                                         [pass] -> run_lint (DETERMINISTIC)
                                               |
                                         [pass] -> run_tests (DETERMINISTIC)
                                               |
                                         [pass] -> create_pr (DETERMINISTIC)
                                               |
8. PR created on GitHub with [Henchmen] prefix and label
   |
9. Forge triggered via Pub/Sub: forge-request
   |
10. Forge clones PR, runs CI, comments results on PR, publishes forge-result
   |
11. Slack notification sent back to the user's thread
```

### Pub/Sub Topic Map

Seven topics, each named `henchmen-{env}-<topic>` (`terraform/modules/pubsub`):

| Topic | Publisher | Subscriber | Delivery |
|-------|-----------|------------|----------|
| `task-intake` | Dispatch | Mastermind `/pubsub/task-intake` | Push |
| `operative-complete` | Operative | Mastermind `/pubsub/operative-complete` | Push |
| `forge-request` | Mastermind | Forge `/pubsub/forge-request` | Push |
| `forge-result` | Forge | Mastermind `/pubsub/forge-result` | Push |
| `ci-failure` | Dispatch (GitHub webhook) | Mastermind `/pubsub/ci-failure` | Push |
| `embed-request` | Dispatch (GitHub push) | none yet (re-embedding hook) | -- |
| `dead-letter` | Pub/Sub (failed deliveries) | Mastermind `/api/v1/check-dlq` pulls `dead-letter-sub` | Pull |

Forge also has a push subscription on Cloud Build's own `cloud-builds` topic (`/pubsub/build-complete`).

All push subscriptions authenticate with an OIDC token minted for the `sa-{env}-pubsub-push` service account, whose audience must match `HENCHMEN_PUBSUB_OIDC_AUDIENCE` on the receiving service.

## Infrastructure

### GCP Services Used

| Service | Purpose |
|---------|---------|
| **Cloud Run (Services)** | Dispatch, Mastermind, Forge |
| **Cloud Run (Jobs)** | Operative Lairs -- ephemeral containers, created per agentic node |
| **Pub/Sub** | Async message passing between all components |
| **Firestore** | Task execution tracking, operative reports, processed-message dedup, merge queue claims |
| **Cloud Storage (GCS)** | Dossier artifacts, Terraform state |
| **Secret Manager** | GitHub token, Slack tokens, Jira API token, metrics bearer token, Dispatch API bearer token |
| **Artifact Registry** | Docker images for all containers |
| **VPC + Serverless VPC Access** | Private-range networking only; public egress does not traverse the VPC |
| **Cloud Scheduler** | Stale-task cleanup, watchdog, dead-letter check, merge queue maintenance (staging/prod; off in dev) |
| **Vertex AI** | LLM access (Gemini only) and RAG Engine corpus (`henchmen-code`) |

### Container Resources

| Container | CPU | Memory | Timeout | Scaling (dev/staging) | Scaling (prod) |
|-----------|-----|--------|---------|-----------------------|----------------|
| Mastermind | 2 vCPU | 4Gi | 3600s | 0-3 | 1-10 |
| Dispatch | 1 vCPU | 512Mi | default | 0-3 | 1-10 |
| Forge | 1 vCPU | 512Mi | default | 0-3 | 1-10 |
| Operative (Lair) | `HENCHMEN_LAIR_DEFAULT_CPU` | `HENCHMEN_LAIR_DEFAULT_MEMORY` | node `timeout_seconds` | n/a (ephemeral) | n/a (ephemeral) |

Lair CPU and memory come from the `lair_cpu` / `lair_memory` Terraform variables, injected into Mastermind as `HENCHMEN_LAIR_DEFAULT_*` (dev: 2 vCPU / 4Gi; the Settings defaults when unset are 4 / 8Gi).

### Terraform Module Structure

```
terraform/
  environments/
    root/              # Shared module composition used by every environment
    dev/               # Wraps root with dev sizing (dev.auto.tfvars) and backend
    staging/           # Wraps root with staging sizing and backend
  modules/
    project-bootstrap/ # Enable required GCP APIs
    networking/        # VPC and Serverless VPC Access connector
    iam/               # Service accounts and project role bindings
    secrets/           # Secret Manager secrets and per-secret accessor IAM
    data-stores/       # Firestore database, indexes, rules; GCS buckets
    pubsub/            # Topics, subscriptions, dead-letter
    artifact-registry/ # Docker image repository
    cloud-run-services/# Mastermind, Dispatch, Forge
    cloud-run-lairs/   # Reference operative job (henchmen-{env}-lair-template)
    cloud-build/       # CI/CD for the Henchmen repo itself
    observability/     # Alerting policies
    vertex-ai/         # Vertex AI configuration
    scheduler/         # Cloud Scheduler jobs
```

### Service Accounts

Project-level roles from `terraform/modules/iam`. Bucket and secret access is granted per resource in `data-stores` and `secrets`.

| Service Account | Key Roles |
|----------------|-----------|
| `sa-{env}-mastermind` | `run.developer`, `pubsub.publisher`, `pubsub.subscriber`, `datastore.user`, `cloudtrace.agent`, `aiplatform.user` (Google publisher models only), `iam.serviceAccountUser` on the operative SA |
| `sa-{env}-dispatch` | `pubsub.publisher`, `cloudtrace.agent` |
| `sa-{env}-operative` | `pubsub.publisher`, `datastore.user`, `cloudtrace.agent`, `aiplatform.user` (Google publisher models only) |
| `sa-{env}-forge` | `cloudbuild.builds.editor`, `pubsub.publisher`, `pubsub.subscriber`, `datastore.user`, `cloudtrace.agent` |
| `sa-{env}-pubsub-push` | `run.invoker` on the services it pushes to |
| `sa-{env}-scheduler` | `run.invoker` on Mastermind and Forge |

Lairs run as `sa-{env}-operative` unless `HENCHMEN_LAIR_SERVICE_ACCOUNT` names another account.

## Model Routing

A scheme node never names a concrete model. Its `model_name` is a **tier**, and the LLM provider selected by `HENCHMEN_LLM_PROVIDER` (falling back to `HENCHMEN_PROVIDER`) resolves it through `henchmen.providers.tiers.resolve_model_name`, reading the matching `Settings` field. The same scheme therefore runs unchanged on Anthropic, OpenAI, Vertex AI, Bedrock or Ollama. A node with no `model_name` defaults to `default/complex`.

| Tier | Used by | Anthropic | OpenAI | Vertex AI |
|------|---------|-----------|--------|-----------|
| `default/complex` | `implement_fix`, `implement_feature` | `claude-sonnet-5` | `gpt-4.1` | `gemini-2.5-pro` |
| `default/light` | planning, classification, RAG reranking, `henchmen chat` | `claude-haiku-4-5` | `gpt-4.1-mini` | `gemini-2.5-flash` |
| `default/reasoning` | `fix_tests`, `analyze_goal` | `claude-opus-5` | `o3` | `gemini-3.1-pro` |

Bedrock reads `HENCHMEN_BEDROCK_MODEL_*`; Ollama uses `HENCHMEN_LLM_OLLAMA_MODEL_<TIER>` and falls back to `HENCHMEN_LLM_OLLAMA_MODEL`.

`verify_changes` and `fix_lint` are deterministic and never call a model: `verify_changes` checks that the branch has source commits ahead of its base, and `fix_lint` runs `ruff check --fix` / `eslint --fix` (`pnpm run lint:fix` in a turbo monorepo).

**Hard rule:** on Vertex AI only Gemini models are used -- no Claude on Vertex AI. Terraform enforces this with an IAM condition that denies non-Google publisher models.
