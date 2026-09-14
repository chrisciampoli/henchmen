# CLAUDE.md — Henchmen

> **Audience:** This file is instructions for AI assistants and LLM-based
> contributors (Claude, Cursor, Copilot-style agents, etc.). It is kept at
> the repo root with the filename `CLAUDE.md` because many AI tools look for
> that exact path.
>
> **Human contributors:** please read `CONTRIBUTING.md` first for workflow,
> PR conventions, and the task completion checklist. The architecture,
> directory layout, and conventions sections below are authoritative for
> both audiences -- humans and AI alike should follow them.

Henchmen is an AI agent factory. It receives tasks from Slack, Jira, GitHub, and CLI, dispatches AI coding agents (Operatives) in ephemeral containers, and delivers human-reviewable pull requests on the configured target repository.

## Quick Start

```bash
pip install -e ".[local,dev]"   # Runtime extras + tooling
henchmen init                    # Interactive setup — writes .env.local
henchmen doctor                  # Verify the environment
pytest tests/unit/               # Run unit tests
ruff check src/ tests/           # Lint
mypy src/                        # Type check
```

## Architecture

Seven components, all villain-themed:

- **Mastermind** (`src/henchmen/mastermind/`) — Orchestrator. Cloud Run service. Tracks the task lifecycle (`TaskStatus`, plus the `execution_state` and heartbeat the stalled-task watchdog reads), selects Schemes, walks their DAG, dispatches Operatives and opens the PR. Fail-closed CI gates: never creates PRs when checks fail.
- **Dispatch** (`src/henchmen/dispatch/`) — Intake router. Cloud Run service. Receives tasks from Slack (Socket Mode), Jira, GitHub, CLI. Normalizes to Task model. Publishes to message broker.
- **Operative** (`src/henchmen/operative/`) — Coding agent. Cloud Run Job. Bootstraps into ephemeral environment, executes Scheme nodes, uses Arsenal tools, reports results. TIMED_OUT stays TIMED_OUT (never upgraded to COMPLETED).
- **Arsenal** (`src/henchmen/arsenal/`) — Tool registry. Runs inside Operative (NOT a separate service). Tool categories: `code_edit`, `code_intel`, `context`, `git_ops`, `github`, `jira`, `slack`, `test_runner`.
- **Forge** (`src/henchmen/forge/`) — CI/merge queue. Cloud Run service. Runs CI on the PR branch Mastermind opened (ruff on changed Python files, tests, silent-failure scan on the diff), comments the results, manages the merge queue.
- **Dossier** (`src/henchmen/dossier/`) — Context builder. Library. Gathers rules, semantic code search via Vertex AI RAG Engine (corpus: `henchmen-code`), task analysis. Caches to object store.
- **Schemes** (`src/henchmen/schemes/`) — DAG workflow blueprints. Library. Defines execution plans: `bugfix_standard`, `feature_standard`, `goal_decomposition`.

Shared data contracts live in **Models** (`src/henchmen/models/`) — Pydantic v2 models for `Task`, `Operative`, `Scheme`, `Dossier`, `LLM` (messages, tool calls, `ModelTier`) and `Evaluation`.

## Task Flow

Topic names are `henchmen-{env}-<topic>` (`Settings.pubsub_topic_*`).

```
Source → Dispatch → Pub/Sub (task-intake) → Mastermind → Dossier (context)
  → Scheme (plan) → Operative (Cloud Run Job) → Arsenal (tools)
  → Pub/Sub (operative-complete) → Mastermind (lint/test gates, create_pr)
  → Pub/Sub (forge-request) → Forge (CI on the PR) → Pub/Sub (forge-result)
  → Human review
```

## Model Tiering

**HARD RULE: No Claude models on Vertex AI. Gemini only.**

Scheme nodes name a *tier*, never a concrete model. The configured LLM provider
resolves it through `henchmen.providers.tiers.resolve_model_name`, so one scheme
runs unchanged on every provider. Never hardcode a model name in a scheme, a
provider, or a price table.

| Tier | Used by | Anthropic | OpenAI | Vertex AI |
|---|---|---|---|---|
| `default/complex` | `implement_fix`, `implement_feature` | `claude-sonnet-5` | `gpt-4.1` | `gemini-2.5-pro` |
| `default/light` | planning, classification | `claude-haiku-4-5` | `gpt-4.1-mini` | `gemini-2.5-flash` |
| `default/reasoning` | `fix_tests`, `analyze_goal` | `claude-opus-5` | `o3` | `gemini-3.1-pro` |

Each cell is a `Settings` field (`anthropic_model_complex`,
`vertex_ai_model_reasoning`, ...). Ollama tiers fall back to
`llm_ollama_model`; Bedrock has its own `bedrock_model_*` fields.

`fix_lint` and `verify_changes` are DETERMINISTIC — no LLM, no Lair. `fix_lint`
runs `ruff check --fix` / `eslint --fix` (`pnpm run lint:fix` in a turbo
monorepo); `verify_changes` checks the branch has source commits ahead of base.

Token pricing lives in exactly one place: `src/henchmen/providers/pricing.py`.
Cost is always computed with `estimate_cost` / `estimate_cost_for_settings`
from that module.

## GCP Services

Cloud Run (services: Dispatch, Mastermind, Forge), Cloud Run Jobs (Operative), Pub/Sub (10 env-prefixed topics with OIDC audience auth), Firestore (state + metrics), GCS (artifacts, TF state), Vertex AI (Gemini for inference, RAG Engine for semantic code search — no Claude on Vertex), Secret Manager, Artifact Registry, Terraform for IaC.

## Language & Stack

- Python 3.12+ — modern typing (`str | None`), async throughout
- FastAPI for HTTP services
- Pydantic v2 with `Field(...)` descriptors for all models
- pydantic-settings with `HENCHMEN_` env prefix, `@lru_cache` singletons
- pytest + pytest-asyncio (`asyncio_mode = "strict"` — every async test needs `@pytest.mark.asyncio`)
- Ruff for linting/formatting (E, F, I, N, W, UP, B, SIM, RET, ASYNC, T20, C4 rules, 120 char line length)
- mypy strict mode for type checking
- Terraform HCL for infrastructure

## Key Conventions

- `str | None` not `Optional[str]`
- `str(uuid4())` for IDs
- `datetime.now(timezone.utc)` for timestamps
- `StrEnum` for string enums
- Module-level docstrings on all files
- snake_case variables/functions, PascalCase classes
- Pydantic models for all data crossing component boundaries — never raw dicts
- Read config from `Settings`, never `os.environ`. The exception is the operative
  runtime contract the Lair injects: `TASK_ID`, `NODE_ID`, `SCHEME_ID`,
  `MODEL_NAME`, `REPO_URL`, `BRANCH`, `TASK_TITLE`, `TASK_DESCRIPTION`,
  `DOSSIER_URI`, `WORKSPACE_DIR`, `OPERATIVE_ID`, `LAIR_ID`
- Credential settings accept both `HENCHMEN_X` and the bare name a Cloud Run
  secret mount injects (`GITHUB_TOKEN`, `SLACK_BOT_TOKEN`, ...) via `AliasChoices`

## Task Completion Checklist

Every task must pass all five before it's done:

```bash
ruff check --fix src/ tests/   # 1. Auto-fix lint
ruff check src/ tests/          # 2. Verify clean
ruff format src/ tests/         # 3. Format
mypy src/                       # 4. Type check
pytest tests/unit/              # 5. Unit tests
```

## Directory Layout

```
henchmen/
├── src/henchmen/              # Main package
│   ├── arsenal/               # Tool registry + tools/ (runs inside the operative)
│   ├── cli/                   # init wizard, doctor, chat, serve, eval
│   ├── config/settings.py     # Pydantic settings (HENCHMEN_ prefix)
│   ├── dispatch/              # Intake router + handlers/ + slack_bot.py
│   ├── dossier/               # Context builder
│   ├── evals/                 # Offline eval harness + SQLite history
│   ├── forge/                 # CI + merge queue
│   ├── mastermind/            # Orchestrator + scheme_executor/
│   ├── models/                # Pydantic data models (task, operative, scheme, dossier, llm, evaluation)
│   ├── observability/         # Cost tracking, metrics API, tracing
│   ├── operative/             # Coding agent
│   ├── providers/             # gcp/ aws/ local/ + anthropic, openai, registry, tiers, pricing
│   ├── schemes/               # DAG workflow blueprints
│   └── utils/                 # git, retry, redaction, stack detection
├── containers/                # Dockerfiles (dispatch, forge, mastermind, operative)
├── evals/fixtures/            # Eval fixtures + baseline.json
├── terraform/                 # IaC (environments, modules)
├── tests/                     # unit/ and integration/, conftest.py
└── pyproject.toml             # Build + tool config
```

## Fail-Closed Principle

Every error/exception path in the scheme executor returns `condition: "fail"`, never `"pass"`:
- Max retry exhaustion → fail + escalate
- Clone failures, missing repo, CI exceptions → fail
- Lair provisioning failure → fail in prod/staging (simulated pass only in dev)
- Missing repo or GitHub token in `create_pr` → fail, never a fabricated PR URL
- A deterministic node with no registered handler → fail
- An undetectable project stack in a CI gate → fail, not "skipped"
- A CI command that cannot run must surface its real exit code; never swallow
  it with `2>/dev/null || echo SKIP`
- A lint gate must only judge files changed by the operative
  (`git diff --name-only origin/<base>`), never pre-existing violations

## Container Build & Deploy

```bash
# Build and push (from repo root)
docker build -f containers/mastermind/Dockerfile \
  -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-${ENV}/mastermind:latest .
docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-${ENV}/mastermind:latest
gcloud run services update henchmen-${ENV}-mastermind \
  --project=${PROJECT_ID} --region=${REGION} \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-${ENV}/mastermind:latest

# Same pattern for: operative, forge, dispatch
# Mastermind creates a fresh `lair-<task>-<node>` job per agentic node from
# operative:${HENCHMEN_LAIR_OPERATIVE_IMAGE_TAG} (default `latest`), so pushing
# the operative image is what changes the operatives. The lair template job is
# a reference/smoke-test copy — keep it in sync so `gcloud run jobs execute`
# tests the same image:
gcloud run jobs update henchmen-${ENV}-lair-template \
  --project=${PROJECT_ID} --region=${REGION} \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-${ENV}/operative:latest
```

## What NOT To Do

- Don't hardcode LLM model names — scheme nodes name a `ModelTier`, providers resolve it from `Settings`
- Don't put logic in Dispatch — it normalizes and publishes, nothing more
- Don't use raw dicts across components — use the Pydantic models
- Don't use `Optional[X]` — use `X | None`
- Don't use naive datetimes — always UTC
- Don't skip the checklist — ruff, mypy, pytest must all pass
- Don't commit secrets — use Secret Manager via settings
- Don't read `os.environ` for anything that has a `Settings` field — the only
  exception is the operative runtime contract listed under Key Conventions
- Don't add a second price table — `providers/pricing.py` is the only one
- Don't commit or push without explicit user permission
- Don't return `condition: "pass"` on errors — always fail-closed
- Don't upgrade TIMED_OUT to COMPLETED — timed out means verification wasn't done
- Don't run `eslint --max-warnings=0` on the whole repo — only lint changed files
- Don't forget OIDC `audience` on Pub/Sub push subscriptions, and inject
  `HENCHMEN_PUBSUB_OIDC_AUDIENCE` to match — a mismatch rejects every push with 401
- Don't let a test reach a live API — `integration_settings` blanks every
  credential on purpose; keep it that way
- Don't use Claude as the git author — all commits must be authored by the human developer
- Don't add Co-Authored-By lines attributing Claude to commits
