# System Architecture

Henchmen Agent Factory is a production AI agent system that dispatches coding operatives to fix bugs and implement features in target repositories. It is inspired by Stripe's Minions architecture: a central orchestrator selects a workflow (Scheme), walks a DAG of deterministic and agentic nodes, provisions ephemeral containers (Lairs) for each agentic node, and opens a pull request with the results.

## High-Level Architecture

```
                        Slack / GitHub / Jira / CLI
                                  |
                          +-------v--------+
                          |    Dispatch     |  Cloud Run Service
                          | (Task Intake)  |  Normalizes webhooks into HenchmenTask
                          +-------+--------+
                                  |  Pub/Sub: task-intake
                                  v
                          +-------+--------+
                          |   Mastermind   |  Cloud Run Service
                          | (Orchestrator) |  Selects scheme, builds dossier,
                          +--+----+----+---+  walks DAG, provisions lairs
                             |    |    |
               +-------------+    |    +-------------+
               |                  |                  |
     +---------v------+  +-------v--------+  +------v----------+
     |  Lair (CRJ)    |  |  Lair (CRJ)    |  |  Lair (CRJ)     |
     | implement_fix  |  | verify_changes |  | fix_tests        |
     | Claude Sonnet 4|  | Gemini Flash   |  | Gemini 3.1 Pro   |
     +---------+------+  +-------+--------+  +------+----------+
               |                  |                  |
               |  Pub/Sub: operative-complete        |
               +------------------+------------------+
                                  |
                                  v
                          +-------+--------+
                          |     Forge      |  Cloud Run Service
                          | (CI Pipeline)  |  Lint, test, silent-failure scan,
                          +-------+--------+  PR comment, merge queue
                                  |
                                  v
                            Pull Request
```

### Component Summary

| Component | Runtime | Purpose |
|-----------|---------|---------|
| **Dispatch** | Cloud Run Service | Ingests tasks from Slack, GitHub, Jira, or CLI. Normalizes into `HenchmenTask` and publishes to Pub/Sub. |
| **Mastermind** | Cloud Run Service | Central orchestrator. Selects a Scheme, builds a Dossier, walks the DAG, provisions Lairs for agentic nodes, creates PRs. |
| **Operative** | Cloud Run Job (Lair) | Ephemeral agent container. Clones repo, runs an agentic tool loop against an LLM, commits and pushes changes. |
| **Arsenal** | In-process registry | Tool system providing `code_intel`, `code_edit`, `git_ops`, and `test_runner` tools to operatives. |
| **Forge** | Cloud Run Service | Post-PR CI pipeline. Clones the PR branch, runs lint/tests/silent-failure detection, comments on PR, publishes results. |
| **Dossier** | Library (in Mastermind) | Context assembly. Fetches file trees, rule files, related PRs/issues, semantic code chunks from Pinecone. |
| **Tracker** | Library (in Mastermind) | Observability layer. Persists per-task and per-node telemetry (tokens, cost, duration) to Firestore. |

## Detailed Component Architecture

### Dispatch

**Source:** `src/henchmen/dispatch/server.py`
**Container:** `containers/dispatch/Dockerfile`
**Cloud Run service:** `henchmen-{env}-dispatch`

Dispatch is the system's front door. It exposes four webhook endpoints:

- `POST /api/v1/tasks` -- CLI task creation (JSON body with title, description, repo)
- `POST /webhooks/slack` -- Slack event subscription (handles `url_verification` challenge and `app_mention` events)
- `POST /webhooks/github` -- GitHub App webhook (issue comments, PR events)
- `POST /webhooks/jira` -- Jira webhook (issue transitions)

Each handler uses a `TaskNormalizer` to convert the source-specific payload into a `HenchmenTask` Pydantic model, then publishes the serialized task to the `henchmen-{env}-task-intake` Pub/Sub topic.

### Mastermind

**Source:** `src/henchmen/mastermind/agent.py`, `scheme_executor.py`, `lair_manager.py`, `state_machine.py`, `server.py`
**Container:** `containers/mastermind/Dockerfile`
**Cloud Run service:** `henchmen-{env}-mastermind`

The Mastermind is the brain of the system. It receives tasks via Pub/Sub push subscription and orchestrates the full execution lifecycle:

1. **Scheme Selection** (`_select_scheme`): Keyword matching on task title/description determines which scheme to use. Goal-level keywords route to `goal_decomposition`, feature keywords to `feature_standard`, bug keywords to `bugfix_standard`.

2. **Dossier Building** (`_build_dossier`): Assembles context for operatives:
   - Fetches the full file tree from GitHub (capped at 200 files)
   - Runs `TaskAnalyzer` to extract mentioned files, error patterns, and keywords
   - Fetches CI failure data if the task is CI-related
   - Pre-fetches file contents for explicitly mentioned files
   - Queries Pinecone for semantically relevant code chunks (RAG)
   - Fetches repo rule files (CLAUDE.md, etc.) and related PRs via `DossierBuilder`

3. **Scheme Execution** (`SchemeExecutor`): Walks the scheme DAG from root to terminal node:
   - **Deterministic nodes** run inline: `create_branch`, `prefetch_context`, `run_lint`, `fix_lint`, `run_tests`, `create_pr`, `escalate`
   - **Agentic nodes** are dispatched to Lairs via `LairManager`
   - Edge conditions (`pass`/`fail`) determine the next node. Unconditional edges are followed as fallback.
   - A per-node retry limit (max 2 executions) prevents infinite loops in controlled retry cycles.

4. **State Machine** (`TaskStateMachine`): Tracks task lifecycle through states: `RECEIVED -> SCHEME_SELECTED -> LAIR_PROVISIONED -> DOSSIER_BUILT -> EXECUTING -> AWAITING_REVIEW -> COMPLETED`. Supports crash recovery by walking transition history backwards.

5. **CI Failure Loop** (`handle_ci_failure`): When CI fails on a Henchmen PR, the Mastermind can dispatch a fix operative (max 2 retries). It extracts errors from GitHub check run annotations and dispatches a new Lair with the error context.

**Pub/Sub endpoints on Mastermind:**
- `POST /pubsub/task-intake` -- Receive new tasks (processes asynchronously)
- `POST /pubsub/operative-complete` -- Receive operative completion reports
- `POST /pubsub/forge-result` -- Receive CI results from Forge
- `POST /pubsub/ci-failure` -- Receive CI failure notifications for auto-fix

### Operative (Lair)

**Source:** `src/henchmen/operative/bootstrap.py`, `agent_builder.py`, `guardrails.py`, `prompt_templates.py`
**Container:** `containers/operative/Dockerfile`
**Cloud Run Job:** Dynamically created per-task by `LairManager`

Each operative runs as an ephemeral Cloud Run Job with a defined lifecycle:

```
SPAWN -> INITIALIZE -> EXECUTE -> REPORT -> TERMINATE
```

**INITIALIZE:** Clones the target repository, checks out a feature branch (`henchmen/{task_id[:8]}`), installs project dependencies (npm/pnpm for Node.js), downloads dossier context from GCS, and pre-reads the most relevant files into context.

**EXECUTE:** Builds an `OperativeAgent` wired with tools from the Arsenal registry. The agent runs an agentic loop:
1. Send conversation (system prompt + task + dossier context) to the LLM
2. LLM returns tool calls or text
3. Execute tool calls via Arsenal handlers
4. Append results and repeat until `git_commit` succeeds or step limit is reached

The agent includes:
- **Phase-aware nudging:** If the model spends too many steps only reading files, it receives an escalating prompt to start editing.
- **Commit detection:** The loop breaks immediately after a successful `git_commit` call.
- **Timeout management:** Agent timeout is `node.timeout_seconds - 120s` (buffer for branch push).
- **Fallback:** Claude calls fall back to Gemini on failure (rate limits, unavailability).

**REPORT:** After execution, the operative checks for changes (uncommitted or committed-ahead-of-main), creates the branch and pushes to origin, then publishes an `OperativeReport` to the `operative-complete` Pub/Sub topic.

**Guardrails** (`OperativeGuardrails`):
- Blocks disallowed tools (only tools matching the node's `ArsenalRequirement` are permitted)
- Blocks path traversal attempts (`../`, `..\\`)
- Truncates oversized messages (>64K chars)
- Enforces step limits
- Tracks token usage and tool call telemetry

### Arsenal

**Source:** `src/henchmen/arsenal/registry.py`, `src/henchmen/arsenal/tools/`

Arsenal is the tool system. It uses a decorator-based registry (`@tool`) to register functions as callable tools. Tools are organized into categories:

| Category | Tools | Description |
|----------|-------|-------------|
| `code_intel` | `file_read`, `file_search`, `symbol_lookup`, `grep_search`, `ast_analysis` | Read-only code exploration |
| `code_edit` | `file_write`, `file_edit`, `file_create`, `file_insert_at_line`, `file_delete` | File modification (file_delete is destructive) |
| `git_ops` | `git_branch_create`, `git_commit`, `git_push`, `git_force_push`, `git_diff`, `git_log`, `git_status` | Git operations (git_force_push is destructive) |
| `test_runner` | `run_tests`, `run_lint`, `type_check` | Test/lint/type-check execution (auto-detects Python vs Node.js) |

Each `SchemeNode` declares an `ArsenalRequirement` specifying which tool sets the operative can access and whether destructive operations are allowed. The `ToolRegistry.get_tools_for_requirement()` method filters accordingly.

Notable tool features:
- `file_edit` supports fuzzy whitespace matching and Unicode normalization (handles LLM-generated smart quotes, em dashes)
- `run_lint` and `run_tests` auto-detect project type (Python uses ruff/pytest/mypy; Node.js uses eslint/jest/tsc)
- Relative paths in tool arguments are automatically resolved to the workspace directory

### Forge

**Source:** `src/henchmen/forge/server.py`, `ci_runner.py`, `silent_failure_detector.py`, `merge_queue.py`, `error_extractor.py`
**Container:** `containers/forge/Dockerfile`
**Cloud Run service:** `henchmen-{env}-forge`

The Forge handles post-PR CI validation:

1. **CI Runner** (`CIRunner`): Clones the PR branch, runs lint (ruff), tests (pytest), and silent failure detection.

2. **Silent Failure Detector** (`SilentFailureDetector`): Scans the git diff for patterns that indicate silent failures:
   - `critical`: Empty catch blocks, bare `except: pass`, hardcoded secrets
   - `warning`: Catch-return-null, catch without logging, retry without backoff, noop changes
   - `info`: TODO/FIXME comments
   - Only critical findings cause the CI check to fail.

3. **Merge Queue** (`MergeQueue`): FIFO merge serialization backed by Firestore. Prevents parallel operatives from creating merge conflicts. Entries transition through states: `pending -> merging -> merged` (or `failed`).

4. **Error Extractor** (`error_extractor.py`): Fetches GitHub check run annotations for failed CI suites and formats them as structured context for fix operatives.

**Pub/Sub endpoints on Forge:**
- `POST /pubsub/forge-request` -- Receive CI run requests (clones branch, runs checks, comments on PR, publishes result)
- `POST /pubsub/build-complete` -- Cloud Build completion callback

### Dossier

**Source:** `src/henchmen/dossier/builder.py`, `rules.py`, `cache.py`, `task_analyzer.py`, `chunker.py`, `embedder.py`

The Dossier subsystem assembles context packages for operatives:

- **DossierBuilder**: Orchestrates fetching relevant files, rule files (CLAUDE.md, .cursorrules, etc.), related PRs, related issues, and code search results. Uploads the assembled dossier as JSON to GCS.
- **TaskAnalyzer**: Classifies tasks by type (bug_fix, test_fix, feature, refactor, generic), extracts mentioned files, error patterns, and keywords using regex patterns.
- **Embedder/Chunker**: Indexes repository code into Pinecone for semantic search (RAG). The Mastermind queries Pinecone for the top 20 chunks relevant to each task.
- **SnapshotCache**: Caches cloned repository snapshots in GCS to speed up workspace initialization.

## Data Flow

### Task Lifecycle

```
1. User posts "fix the login bug" in Slack
   |
2. Dispatch receives Slack event, normalizes to HenchmenTask
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
   create_branch -> prefetch_context -> implement_fix (AGENTIC)
                                               |
                                         [Lair provisioned]
                                         [Operative runs Claude Sonnet 4]
                                         [Reads code, makes fix, commits]
                                               |
                                         verify_changes (AGENTIC, Gemini Flash)
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
10. Forge clones PR, runs CI, comments results on PR
   |
11. Slack notification sent back to the user's thread
```

### Pub/Sub Topic Map

| Topic | Publisher | Subscriber | Delivery |
|-------|-----------|------------|----------|
| `task-intake` | Dispatch | Mastermind | Push |
| `task-planned` | Mastermind | Dispatch | Push |
| `operative-dispatch` | Mastermind | Lair launcher | Pull (exactly-once) |
| `operative-status` | Operative | (informational) | Pull |
| `operative-complete` | Operative | Mastermind | Push |
| `forge-request` | Mastermind | Forge | Push |
| `forge-result` | Forge | Mastermind | Push |
| `ci-failure` | GitHub webhook | Mastermind | Push |
| `dead-letter` | Pub/Sub (failed deliveries) | Monitoring | Pull (exactly-once) |

All push subscriptions use OIDC authentication tokens with the Mastermind service account as the invoker.

## Infrastructure

### GCP Services Used

| Service | Purpose |
|---------|---------|
| **Cloud Run (Services)** | Dispatch, Mastermind, Arsenal, Forge -- always-on HTTP services |
| **Cloud Run (Jobs)** | Operative Lairs -- ephemeral containers, dynamically created per task |
| **Pub/Sub** | Async message passing between all components |
| **Firestore** | Task execution tracking, merge queue state, operative reports |
| **Cloud Storage (GCS)** | Dossier artifacts, Terraform state, operative snapshots |
| **Secret Manager** | GitHub token, Slack tokens, Pinecone API key, Jira API token |
| **Artifact Registry** | Docker images for all containers |
| **VPC Connector** | Private networking for Cloud Run services |
| **Cloud Scheduler** | Periodic cleanup and merge queue processing |
| **Vertex AI** | LLM access (Claude via Anthropic-on-Vertex, Gemini native) |

### Container Resources

| Container | CPU | Memory | Timeout | Scaling (dev) | Scaling (prod) |
|-----------|-----|--------|---------|---------------|----------------|
| Mastermind | 2 vCPU | 4Gi | 3600s | 0-3 | 1-10 |
| Dispatch | 1 vCPU | 512Mi | default | 0-3 | 1-10 |
| Arsenal | 1 vCPU | 512Mi | default | 0-3 | 1-10 |
| Forge | 1 vCPU | 512Mi | default | 0-3 | 1-10 |
| Operative (Lair) | 4 vCPU | 8Gi | per-node | n/a (ephemeral) | n/a (ephemeral) |

Operative Lairs scale up to 4 vCPU / 8Gi for nodes with timeout > 300s.

### Terraform Module Structure

```
terraform/
  environments/
    dev/
      main.tf          # Wires all modules together
      variables.tf     # Project ID, region, environment
      backend.tf       # GCS backend for state
  modules/
    project-bootstrap/ # Enable required GCP APIs
    networking/        # VPC connector
    iam/               # Service accounts and role bindings
    secrets/           # Secret Manager secrets and IAM
    data-stores/       # Firestore database and indexes
    pubsub/            # Topics, subscriptions, dead-letter
    artifact-registry/ # Docker image repository
    cloud-run-services/# Mastermind, Arsenal, Dispatch, Forge
    cloud-run-lairs/   # Operative job template
    cloud-build/       # CI/CD for the Henchmen repo itself
    observability/     # Alerting policies
    vertex-ai/         # Vertex AI endpoint configuration
    scheduler/         # Cloud Scheduler jobs
```

### Service Accounts

| Service Account | Key Roles |
|----------------|-----------|
| `sa-{env}-mastermind` | `run.invoker`, `pubsub.publisher`, `pubsub.subscriber`, `datastore.user`, `aiplatform.user`, `run.developer` |
| `sa-{env}-dispatch` | `run.invoker`, `pubsub.publisher` |
| `sa-{env}-operative` | `pubsub.publisher`, `datastore.viewer`, `storage.objectAdmin` |
| `sa-{env}-arsenal` | `run.invoker` |
| `sa-{env}-forge` | `cloudbuild.builds.editor`, `pubsub.publisher`, `pubsub.subscriber`, `datastore.user`, `storage.objectViewer` |
| `sa-{env}-dossier` | `storage.objectAdmin`, `datastore.viewer` |

## Model Routing

The system uses a tiered model strategy to balance cost and quality:

| Model | Used For | Why |
|-------|----------|-----|
| **Claude Sonnet 4** (`claude-sonnet-4@20250514`) | `implement_fix`, `implement_feature` | Best-in-class coding quality. Used for the core code generation step where accuracy matters most. Accessed via Anthropic-on-Vertex in `us-east5`. |
| **Gemini 3.1 Pro** (`gemini-3.1-pro`) | `fix_tests` | Strong reasoning for diagnosing test failures and making targeted fixes. Cost-effective for the retry step. |
| **Gemini 3.1 Pro Preview** (`gemini-3.1-pro-preview-customtools`) | `analyze_goal` (goal decomposition) | Planning and analysis of high-level goals with custom tool support. |
| **Gemini 2.5 Flash** (`gemini-2.5-flash`) | `verify_changes`, `plan_implementation` | Fast and cheap for verification gates and planning steps. Low-latency turnaround for quality checks. |

The `model_name` field on each `SchemeNode` determines which model is used. If not set, it falls back to the `vertex_ai_model_complex` setting (Claude Sonnet 4 by default). The operative's `_call_model` method routes to `_call_claude` or `_call_gemini` based on the model name prefix, with automatic fallback from Claude to Gemini on failure.
