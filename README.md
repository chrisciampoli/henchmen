<div align="center">

# HENCHMEN

**AI Agent Factory. Cloud-Agnostic. Villain-Themed.**

[![License](https://img.shields.io/badge/license-Apache%202.0-7c3aed)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-3776ab)](https://python.org)
[![CI](https://github.com/chrisciampoli/henchmen/actions/workflows/ci.yml/badge.svg)](https://github.com/chrisciampoli/henchmen/actions/workflows/ci.yml)

Submit a task. Get a pull request.

[Quick Start](#-quick-start) · [Architecture](#-architecture) · [Providers](#-provider-support) · [Contributing](CONTRIBUTING.md)

</div>

---

Henchmen receives tasks from **Slack, GitHub, Jira, and CLI**, dispatches AI coding agents in **ephemeral containers**, and delivers **human-reviewable pull requests**. It runs on GCP, AWS, or locally with zero cloud dependencies.

---

## Verified today

A real-looking AI agent factory is easy to claim, hard to prove. Here are
the concrete artifacts you can inspect right now to evaluate Henchmen
without deploying anything:

- **Green CI** — [![CI](https://github.com/chrisciampoli/henchmen/actions/workflows/ci.yml/badge.svg)](https://github.com/chrisciampoli/henchmen/actions/workflows/ci.yml) All 5 jobs (lint, typecheck, unit, integration, compose smoke) pass on every commit to `main`. 887 tests, 0 skipped.
- **Expert-panel review** — [`docs/superpowers/reviews/`](docs/superpowers/reviews/) contains the full 8-expert OSS readiness review (82 findings, all closed) that produced this release.
- **Deploy-on-your-own-GCP walkthrough** — [`docs/deploy-gcp.md`](docs/deploy-gcp.md) is a 30-minute linear walkthrough from `gcloud auth login` to a live stack processing CLI tasks.
- **Reproducible BYO-LLM parity** — [`evals/baseline.json`](evals/baseline.json) is a structured stub with per-provider populate commands. [`.github/workflows/evals.yml`](.github/workflows/evals.yml) is a `workflow_dispatch` workflow that runs the eval harness against one provider and opens a PR with the updated baseline.
- **Supply-chain integrity** — all 4 Dockerfiles pin base images to sha256 digests; the [release workflow](.github/workflows/release.yml) builds and publishes release artifacts to GHCR with signed provenance (SLSA L1).
- **Self-diagnosis** — `henchmen doctor` runs a local self-check (Docker, git, LLM credentials, operative image, Python version) before you spin anything up.
- **Metrics sample** — [`docs/images/metrics-sample.txt`](docs/images/metrics-sample.txt) shows the Prometheus-format output the observability stack produces when the eval harness runs against three fixtures.

The maintainer has executed the deploy-gcp walkthrough end-to-end
against a real GCP project. The eval baseline is intentionally left as
a stub — each contributor runs the harness on their own hardware /
account and opens a PR with their numbers via the evals workflow.

---

## Prerequisites

| Requirement | Why |
|---|---|
| **Python 3.12+** | Runtime for all services |
| **Docker** | Local mode runs agents as containers |
| **Git** | Operatives clone, branch, and push |
| **GitHub App** | Henchmen creates branches and PRs on your repo ([setup guide](#-github-app-setup)) |

---

## Quick Start

### Option A: Docker Compose (recommended)

```bash
git clone https://github.com/chrisciampoli/henchmen.git
cd henchmen
pip install -e ".[dev]"
cp .env.example .env.local
docker compose up
```

Then submit your first task:

```bash
curl -X POST http://localhost:8001/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Fix the login bug",
    "description": "Users cannot log in after password reset",
    "source": "cli",
    "repo": "your-org/your-repo"
  }'
```

> **Note:** For the full PR workflow (branch, push, create PR), you need a GitHub App configured. Without one, you can still test task intake, LLM reasoning, and scheme execution in dry-run mode.

### Option B: Single Process (no Docker)

```bash
pip install -e "."
henchmen serve
```

This runs Dispatch + Mastermind + Forge in one process with in-memory queues and SQLite. You'll need [Ollama](https://ollama.com) running locally for LLM inference:

```bash
# In another terminal
ollama serve
ollama pull llama3.2
```

---

## How It Works

```
1. You submit a task          (Slack, GitHub issue, Jira ticket, or CLI)
2. Dispatch normalizes it     (unified Task model, publishes to message broker)
3. Mastermind plans the work  (selects a Scheme, builds a Dossier with RAG context)
4. Operative executes         (ephemeral container, LLM + Arsenal tools, commits code)
5. Forge runs CI              (lint, tests, builds the PR)
6. You review the PR          (human-in-the-loop, always)
```

---

## Architecture

```mermaid
graph LR
    A["Source\nSlack · GitHub · Jira · CLI"] --> B["Dispatch\n(intake router)"]
    B --> C["Mastermind\n(orchestrator)"]
    C --> D["Dossier\n(RAG + context)"]
    C --> E["Schemes\n(workflow DAG)"]
    E --> F["Operative\n(coding agent)"]
    F --> G["Arsenal\n(code tools)"]
    F --> H["Forge\n(CI + PR)"]
    H --> I["Pull Request\n(ready for review)"]

    style A fill:#1a1a2e,stroke:#7c3aed,color:#e2e8f0
    style C fill:#1a1a2e,stroke:#7c3aed,color:#e2e8f0
    style F fill:#1a1a2e,stroke:#7c3aed,color:#e2e8f0
    style I fill:#1a1a2e,stroke:#34d399,color:#e2e8f0
```

---

## Components

| Component | Path | Role |
|---|---|---|
| **Mastermind** | `src/henchmen/mastermind/` | Orchestrator. State machine, scheme selection, operative dispatch. Fail-closed CI gates. |
| **Dispatch** | `src/henchmen/dispatch/` | Intake router. Normalizes tasks from all sources into a unified Task model. |
| **Operative** | `src/henchmen/operative/` | Coding agent. Ephemeral container. Executes scheme nodes with Arsenal tools. |
| **Arsenal** | `src/henchmen/arsenal/` | Tool registry. `code_edit`, `code_intel`, `git_ops`, `test_runner`, `github`. |
| **Forge** | `src/henchmen/forge/` | CI + merge queue. Runs lint/tests, builds PRs, detects silent failures. |
| **Dossier** | `src/henchmen/dossier/` | Context builder. Rules, RAG semantic search, task analysis. Caches to object store. |
| **Schemes** | `src/henchmen/schemes/` | DAG workflow blueprints: `bugfix_standard`, `feature_standard`, `goal_decomposition`. |

---

## Provider Support

Henchmen is built on 6 provider interfaces. Swap any layer independently.

| Interface | GCP (supported) | Local (supported) | AWS (experimental) | OpenAI | Anthropic |
|---|:---:|:---:|:---:|:---:|:---:|
| **MessageBroker** | Pub/Sub | in-memory | SNS + SQS | -- | -- |
| **DocumentStore** | Firestore | SQLite | DynamoDB | -- | -- |
| **ObjectStore** | GCS | filesystem | S3 | -- | -- |
| **ContainerOrchestrator** | Cloud Run Jobs | Docker | ECS Fargate | -- | -- |
| **LLMProvider** | Vertex AI (Gemini) | Ollama | Bedrock | OpenAI API | Anthropic API |
| **CIProvider** | Cloud Build | shell | CodeBuild | -- | -- |

**Supported providers** (GCP, Local) have end-to-end walkthroughs, full
integration coverage, and an exercised release path. See
[`docs/deploy-gcp.md`](docs/deploy-gcp.md) for the GCP self-host guide.

**Experimental providers** (AWS) ship with unit tests for every
interface but have not yet been exercised end-to-end by the maintainer.
They are kept alive for community contributions — if you want to run
Henchmen on AWS, start a thread in
[GitHub Discussions](https://github.com/chrisciampoli/henchmen/discussions)
and the maintainer will pair with you on a first-run walkthrough.

Set your provider:

```bash
HENCHMEN_PROVIDER=local    # Local Docker + Ollama (default for dev)
HENCHMEN_PROVIDER=gcp      # Google Cloud Platform — see docs/deploy-gcp.md
HENCHMEN_PROVIDER=aws      # AWS (experimental — community supported)
```

Override individual services:

```bash
HENCHMEN_PROVIDER=gcp
HENCHMEN_LLM_PROVIDER=openai   # Use OpenAI for LLM, GCP for everything else
```

---

## Installation

```bash
pip install -e "."                  # Core only (no cloud SDKs)
pip install -e ".[gcp]"            # + GCP providers
pip install -e ".[aws]"            # + AWS providers
pip install -e ".[openai]"         # + OpenAI LLM
pip install -e ".[anthropic]"      # + Anthropic LLM
pip install -e ".[all]"            # Everything
pip install -e ".[dev]"            # Everything + test/lint tools
```

---

## Configuration

All settings use the `HENCHMEN_` prefix. Copy the example to get started:

```bash
cp .env.example .env.local
```

Key settings:

| Variable | Description | Default |
|---|---|---|
| `HENCHMEN_PROVIDER` | Default cloud provider | `gcp` |
| `HENCHMEN_ENVIRONMENT` | Deployment environment | `dev` |
| `HENCHMEN_GITHUB_DEFAULT_REPO` | Target repository (owner/repo) | *(required)* |
| `HENCHMEN_GIT_AUTHOR_EMAIL` | Git author for operative commits | `henchmen-operative@noreply.local` |
| `HENCHMEN_LLM_OLLAMA_MODEL` | Default Ollama model (local mode) | `llama3.2` |

See [`src/henchmen/config/settings.py`](src/henchmen/config/settings.py) for the full reference.

---

<details>
<summary><strong>GitHub App Setup</strong></summary>

Henchmen needs a GitHub App to create branches and pull requests on your target repo.

1. Go to **Settings > Developer settings > GitHub Apps > New GitHub App**
2. Set these permissions:
   - **Repository permissions:**
     - Contents: Read & Write
     - Pull requests: Read & Write
     - Issues: Read
3. Install the app on your target repository
4. Generate a private key and download the `.pem` file
5. Set in your `.env.local`:

```bash
HENCHMEN_GITHUB_APP_ID=123456
HENCHMEN_GITHUB_APP_PRIVATE_KEY_SECRET=/path/to/private-key.pem
HENCHMEN_GITHUB_DEFAULT_ORG=your-org
HENCHMEN_GITHUB_DEFAULT_REPO=your-org/your-repo
```

</details>

<details>
<summary><strong>Slack App Setup</strong></summary>

Henchmen uses Slack Socket Mode to receive tasks from Slack messages. When someone @mentions the bot, Dispatch normalizes the message into a task.

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app **From scratch**
2. Under **Socket Mode**, enable it and generate an **App-Level Token** with `connections:write` scope
3. Under **OAuth & Permissions**, add these **Bot Token Scopes:**
   - `app_mentions:read` — receive @mentions
   - `chat:write` — post status updates back to channels
   - `channels:history` — read thread context for richer task descriptions
4. Under **Event Subscriptions**, enable events and subscribe to:
   - `app_mention` — triggers task creation when someone @mentions the bot
5. Install the app to your workspace
6. Set in your `.env.local`:

```bash
HENCHMEN_SLACK_BOT_TOKEN_SECRET=xoxb-your-bot-token
HENCHMEN_SLACK_APP_TOKEN_SECRET=xapp-your-app-level-token
HENCHMEN_SLACK_SIGNING_SECRET=your-signing-secret
HENCHMEN_SLACK_NOTIFICATION_CHANNEL=C0123CHANNEL  # Channel ID for status updates
```

> **How it works:** A user posts `@Henchmen fix the login bug in auth.py` in any channel the bot is in. Dispatch picks up the mention, pulls thread context if available, normalizes it into a Task with `source=slack`, and publishes it to the message broker. Mastermind takes it from there.

</details>

<details>
<summary><strong>Jira Integration Setup</strong></summary>

Henchmen can receive tasks from Jira webhooks when issues are created or transitioned.

1. In your Jira project, go to **Settings > Webhooks > Create Webhook**
2. Set the URL to your Dispatch endpoint: `https://your-dispatch-url/webhooks/jira`
3. Select events: **Issue created**, **Issue updated**
4. Set in your `.env.local`:

```bash
HENCHMEN_JIRA_BASE_URL=https://your-org.atlassian.net
HENCHMEN_JIRA_EMAIL=your-service-account@your-org.com
HENCHMEN_JIRA_API_TOKEN_SECRET=your-jira-api-token
HENCHMEN_JIRA_PROJECT_KEY=PROJ
```

</details>

---

## Development

```bash
ruff check src/ tests/          # Lint
ruff format src/ tests/         # Format
mypy src/                       # Type check
pytest tests/unit/              # Unit tests
```

All four must pass before submitting a PR.

---

## Troubleshooting

**`henchmen-operative:local` image build fails**
Check that Docker Desktop is running and has at least 4GB of RAM allocated. Try `henchmen build-operative --no-cache` to force a clean rebuild. On slow networks, first build can take 5–10 minutes.

**Task dispatched but nothing happens**
Check the server terminal for errors. Verify Docker is running: `docker ps`. Make sure `GITHUB_TOKEN` is set in `.env.local` and has `repo` scope (classic PAT, not fine-grained).

**`401 Unauthorized` when creating PR**
Your `GITHUB_TOKEN` is missing the `repo` scope or has expired. Create a new classic token at https://github.com/settings/tokens with `repo` checked.

**Operative container runs but PR is empty / wrong files**
Likely an LLM tool-calling failure. If you're using Ollama with a model smaller than 14B, switch to OpenAI/Anthropic or pull a larger Ollama model. See `HENCHMEN_LLM_PROVIDER` in `.env.local`.

**`Connection refused` to Ollama**
The operative container calls Ollama at `http://host.docker.internal:11434`. Make sure `ollama serve` is running on the host. Verify with `curl http://localhost:11434/api/tags`.

**Linux: `permission denied` on Docker socket**
Add your user to the `docker` group: `sudo usermod -aG docker $USER`, then log out and back in.

**PR was opened but CI steps were skipped**
Expected in local mode. Mastermind detects the target repo's stack and runs the appropriate test/lint commands locally, but Forge runs the authoritative CI on the created PR via the full pipeline. Local mode short-circuits this to let you see the PR faster.

See [docs/troubleshooting.md](docs/troubleshooting.md) for the full 15-scenario guide.

---

## Supported Languages

Henchmen detects the target repository's stack at runtime via manifest
files and runs the appropriate lint / test commands. The following
stacks are detected and supported out of the box:

| Stack        | Detected by                             | Test command                         | Lint command             |
|--------------|-----------------------------------------|--------------------------------------|--------------------------|
| Python       | `pyproject.toml`, `setup.py`, `requirements.txt` | `python -m pytest`          | `python -m ruff check`   |
| Node (pnpm)  | `pnpm-lock.yaml` (+ `turbo.json` for monorepos) | `pnpm run test`             | `pnpm run lint`          |
| Node (npm)   | `package.json` (no pnpm lockfile)       | `npm test`                           | `eslint` on changed files |
| Go           | `go.mod`                                | `go test ./...`                      | `go vet ./...`           |
| Rust         | `Cargo.toml`                            | `cargo test`                         | `cargo clippy`           |
| Java (Maven) | `pom.xml`                               | `mvn test`                           | `mvn verify -DskipTests` |
| Java (Gradle)| `build.gradle` or `build.gradle.kts`    | `./gradlew test`                     | `./gradlew check -x test`|

If your project uses something else, the run_tests handler falls back
gracefully with a "stack not detected" skip rather than failing. See
`src/henchmen/utils/stack_detector.py` for the detection logic and add
a new stack via a pull request.

---

## Reproducibility: verify BYO-LLM parity yourself

Henchmen supports 5 LLM providers (Vertex AI Gemini, AWS Bedrock,
OpenAI, Anthropic, and Ollama). To measure how close your chosen
provider gets to the Gemini 2.5 Pro baseline on your own hardware /
account, run the eval harness:

```bash
# Run a single fixture against the provider of your choice.
henchmen eval --provider openai --fixture bugfix_off_by_one

# Run the whole fixture set (3 fixtures by default — add your own in
# evals/fixtures/).
henchmen eval --provider ollama --all
```

Results go to `evals/baseline.json`. A stub file ships in the repo with
a `"how to populate me"` hint next to every provider entry. If you want
to publish your numbers, open a PR updating the stub with your results.

The [`.github/workflows/evals.yml`](.github/workflows/evals.yml)
workflow is `workflow_dispatch`-triggered so you can run it against
your own GitHub Actions runner with your own secrets — it opens a PR
updating `evals/baseline.json` for review.

---

## Documentation

- [Architecture](docs/architecture.md)
- [Schemes](docs/schemes.md)
- [Cost Model](docs/cost-model.md)
- [Troubleshooting](docs/troubleshooting.md)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for code style, PR process, and how to add new providers.

## License

[Apache 2.0](LICENSE)
