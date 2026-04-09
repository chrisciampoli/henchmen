# CLAUDE.md — Henchmen

Henchmen is an AI agent factory. It receives tasks from Slack, Jira, GitHub, and CLI, dispatches AI coding agents (Operatives) in ephemeral containers, and delivers human-reviewable pull requests on the configured target repository.

## Quick Start

```bash
pip install -e ".[dev]"     # Install with dev dependencies
pytest tests/unit/           # Run unit tests
ruff check src/ tests/       # Lint
mypy src/                    # Type check
```

## Architecture

Seven components, all villain-themed:

- **Mastermind** (`src/henchmen/mastermind/`) — Orchestrator. Cloud Run service. Manages task lifecycle via state machine, selects Schemes, dispatches Operatives. Fail-closed CI gates: never creates PRs when checks fail.
- **Dispatch** (`src/henchmen/dispatch/`) — Intake router. Cloud Run service. Receives tasks from Slack (Socket Mode), Jira, GitHub, CLI. Normalizes to Task model. Publishes to message broker.
- **Operative** (`src/henchmen/operative/`) — Coding agent. Cloud Run Job. Bootstraps into ephemeral environment, executes Scheme nodes, uses Arsenal tools, reports results. TIMED_OUT stays TIMED_OUT (never upgraded to COMPLETED).
- **Arsenal** (`src/henchmen/arsenal/`) — Tool registry. Runs inside Operative (NOT a separate service). Provides tools: `code_edit`, `code_intel`, `github`, `git_ops`, `test_runner`.
- **Forge** (`src/henchmen/forge/`) — CI/merge queue. Cloud Run service. Orchestrates CI, builds PRs, manages merge queue, detects silent failures.
- **Dossier** (`src/henchmen/dossier/`) — Context builder. Library. Gathers rules, RAG via Pinecone (index: `henchmen-code`), task analysis. Caches to object store.
- **Schemes** (`src/henchmen/schemes/`) — DAG workflow blueprints. Library. Defines execution plans: `bugfix_standard`, `feature_standard`, `goal_decomposition`.

Shared data contracts live in **Models** (`src/henchmen/models/`) — Pydantic v2 models for `Task`, `Operative`, `Scheme`, `Dossier`.

## Task Flow

```
Source → Dispatch → Pub/Sub (tasks.created) → Mastermind → Dossier (context)
  → Scheme (plan) → Operative (Cloud Run Job) → Arsenal (tools)
  → Pub/Sub (operative.complete) → Forge (CI + PR) → Human review
```

## Model Tiering

**HARD RULE: No Claude models on Vertex AI. Gemini only.**

- `implement_fix` / `implement_feature` → Gemini 2.5 Pro (`gemini-2.5-pro`) — core coding
- `fix_tests` → Gemini 3.1 Pro (`gemini-3.1-pro`) — needs reasoning
- `verify_changes` / `plan_implementation` → Gemini 2.5 Flash (`gemini-2.5-flash`) — 95% cheaper
- `fix_lint` → DETERMINISTIC (`eslint --fix` / `ruff --fix`) — zero LLM cost, no Cloud Run Job

## GCP Services

Cloud Run (services: Dispatch, Mastermind, Forge), Cloud Run Jobs (Operative), Pub/Sub (8 env-prefixed topics with OIDC audience auth), Firestore (state + metrics), GCS (artifacts, TF state), Vertex AI (Gemini only — no Claude), Pinecone (RAG semantic search), Secret Manager, Artifact Registry, Terraform for IaC.

## Language & Stack

- Python 3.12+ — modern typing (`str | None`), async throughout
- FastAPI for HTTP services
- Pydantic v2 with `Field(...)` descriptors for all models
- pydantic-settings with `HENCHMEN_` env prefix, `@lru_cache` singletons
- pytest + pytest-asyncio (`asyncio_mode = "auto"`)
- Ruff for linting/formatting (E, F, I, N, W, UP rules, 120 char line length)
- mypy strict mode for type checking
- Terraform HCL for infrastructure

## Key Conventions

- `str | None` not `Optional[str]`
- `str(uuid4())` for IDs
- `datetime.now(timezone.utc)` for timestamps
- `str, Enum` pattern for string enums
- Module-level docstrings on all files
- snake_case variables/functions, PascalCase classes
- Pydantic models for all data crossing component boundaries — never raw dicts

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
│   ├── arsenal/               # MCP tool registry + tools/
│   ├── config/settings.py     # Pydantic settings (HENCHMEN_ prefix)
│   ├── dispatch/              # Intake router + handlers/
│   ├── dossier/               # Context builder
│   ├── forge/                 # CI + merge queue
│   ├── mastermind/            # Orchestrator
│   ├── models/                # Pydantic data models (task, operative, scheme, dossier)
│   ├── operative/             # Coding agent
│   └── schemes/               # DAG workflow blueprints
├── containers/                # Dockerfiles (dispatch, forge, mastermind, operative)
├── terraform/                 # IaC (environments, modules, shared)
├── tests/                     # unit/ and integration/, conftest.py
└── pyproject.toml             # Build + tool config
```

## Fail-Closed Principle

Every error/exception path in the scheme executor returns `condition: "fail"`, never `"pass"`:
- Max retry exhaustion → fail + escalate
- Clone failures, missing repo, CI exceptions → fail
- Lair provisioning failure → fail in prod/staging (simulated pass only in dev)
- Lint checks only run on files changed by the operative (`git diff --name-only origin/main`)

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
# After operative rebuild, also update the lair template:
gcloud run jobs update henchmen-${ENV}-lair-template \
  --project=${PROJECT_ID} --region=${REGION} \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/henchmen-${ENV}/operative:latest
```

## What NOT To Do

- Don't hardcode LLM model names — use `HENCHMEN_` settings or scheme node `model_name`
- Don't put logic in Dispatch — it normalizes and publishes, nothing more
- Don't use raw dicts across components — use the Pydantic models
- Don't use `Optional[X]` — use `X | None`
- Don't use naive datetimes — always UTC
- Don't skip the checklist — ruff, mypy, pytest must all pass
- Don't commit secrets — use Secret Manager via settings
- Don't commit or push without explicit user permission
- Don't return `condition: "pass"` on errors — always fail-closed
- Don't upgrade TIMED_OUT to COMPLETED — timed out means verification wasn't done
- Don't run `eslint --max-warnings=0` on the whole repo — only lint changed files
- Don't forget OIDC `audience` on Pub/Sub push subscriptions — causes silent 403s
- Don't use Claude as the git author — all commits must be authored by the human developer
- Don't add Co-Authored-By lines attributing Claude to commits
