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

## Prerequisites

| Requirement | Why |
|---|---|
| **Python 3.12+** | Runtime for all services |
| **Docker** | Operatives run as containers, in local mode too |
| **Git** | Operatives clone, branch, and push |
| **A GitHub token** | A classic PAT with the `repo` scope, from an account that can push to your target repo |
| **An LLM key** | Anthropic, OpenAI, Vertex AI, Bedrock, or a local Ollama server |

---

## Quick Start

```bash
git clone https://github.com/chrisciampoli/henchmen.git
cd henchmen
pip install -e ".[local,dev]"

henchmen init              # interactive setup — writes .env.local
henchmen doctor            # verify everything it just configured
henchmen config --only-set # optional: the effective settings, credentials masked
henchmen build-operative   # build the operative image (first run, ~3 min)
henchmen serve             # Dispatch + Mastermind + Forge in one process
```

`henchmen init` walks you through deployment mode, LLM provider and per-tier
models, GitHub, Slack and Jira. It checks each credential against the real API
before writing it, lists the models your key can actually reach, and shows your
Slack channels so you can pick the one the bot should join. Re-run it any time;
existing values become the defaults, and `--section llm` changes just one part.

Then describe a task in your own words:

```bash
henchmen chat
```

or post one directly:

```bash
curl -X POST http://localhost:8000/dispatch/api/v1/tasks \
  -H "Authorization: Bearer $HENCHMEN_DISPATCH_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Fix the login bug",
    "description": "Users cannot log in after a password reset",
    "repo": "your-org/your-repo",
    "task_type": "bugfix"
  }'
```

`POST /api/v1/tasks` requires `Authorization: Bearer <HENCHMEN_DISPATCH_API_TOKEN>`.
The header is optional in dev while the token is empty (Dispatch logs a warning
and accepts the request); in staging and prod an empty token makes the route
return 401. `henchmen chat` sends the header for you.

`task_type` is optional: `bugfix`, `feature` or `refactor`. When set it picks
the scheme (`bugfix_standard`, or `feature_standard` for feature and refactor)
instead of keyword matching on the text. Goal phrases in the title such as
"improve" or "fix all" still route to `goal_decomposition` first.

### CLI

| Command | What it does |
|---|---|
| `henchmen init [--section ...]` | Interactive setup; writes `.env.local` |
| `henchmen doctor [--offline]` | Checks the environment, credentials, tier models and their prices |
| `henchmen config [--only-set]` | Prints the effective settings, credentials masked |
| `henchmen build-operative [--no-cache]` | Builds the local `henchmen-operative:local` image |
| `henchmen serve` | Runs Dispatch, Mastermind and Forge in one process, including each service's startup (the Slack bot connects, the metrics routes are mounted) |
| `henchmen chat` | Describe a task conversationally, then submit it to Dispatch |
| `henchmen embed <owner/repo> [--full]` | Indexes a repository into the RAG Engine corpus. Incremental by default; `--full` clears the repo's existing chunks and re-indexes every file. Exits non-zero unless the run completed |
| `henchmen eval run / compare / history` | Offline eval harness (see [`evals/README.md`](evals/README.md)) |

On GCP, Dispatch also requests an incremental re-index whenever GitHub reports a
push to the repository's default branch; Mastermind runs it from the
`embed-request` topic.

### Docker Compose

`docker compose up` runs the same single-process server plus an Ollama
sidecar. It mounts the Docker socket so the server can launch operative
containers. Configure `.env.local` first — `henchmen init` is the easy way.

### Local image (preview)

The all-in-one image the upcoming Henchmen Desktop app runs. This Phase 1
preview starts the image in setup mode and demonstrates the secured Console —
the sign-in link, status and guide-progress endpoints — but the guided setup
screens that complete it ship in the next release, so there is currently no
way to finish setup or run a task from this image. For a working local run
today, use the [CLI](#cli) or [Docker Compose](#docker-compose) above instead.

```bash
docker network create henchmen
docker run -d --name henchmen --network henchmen --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v henchmen-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e HENCHMEN_LOCAL_DOCKER_NETWORK=henchmen \
  -e HENCHMEN_LOCAL_FORWARD_BASE_URL=http://henchmen:8000 \
  -e HENCHMEN_OPERATIVE_IMAGE=ghcr.io/chrisciampoli/henchmen/operative:latest \
  ghcr.io/chrisciampoli/henchmen/local:latest
docker logs -f henchmen
```

`HENCHMEN_LOCAL_DOCKER_NETWORK=henchmen` and `HENCHMEN_LOCAL_FORWARD_BASE_URL=http://henchmen:8000`,
together with `--network henchmen --name henchmen`, are required exactly as shown: they are how an
operative container calls back to this one by name. Leave any of them out (or point
`HENCHMEN_LOCAL_FORWARD_BASE_URL` at a loopback address) and Henchmen still starts, but opens in
needs-attention mode with a named fix, since the whole-app Host allowlist would otherwise silently
refuse the callback.

Look for the line starting `Open Henchmen setup:` and open it. A link can be used once; for a fresh
one, run `docker exec henchmen henchmen console-link`. A launcher should always get its link from
`henchmen console-link` rather than rely on `HENCHMEN_CONSOLE_SETUP_TOKEN`: that value only seeds the
first token a data directory ever issues, so a `console-link` run before the first start uses it up.

### Prebuilt images

Each release publishes `ghcr.io/chrisciampoli/henchmen/{dispatch,mastermind,forge,operative,local}`
tagged `X.Y.Z` and `latest`. See
[Prebuilt images](docs/deploy-gcp.md#prebuilt-images) for pulling them and
copying them into Artifact Registry for Cloud Run.

---

## How It Works

```
1. You submit a task          (Slack, GitHub issue, Jira ticket, or CLI)
2. Dispatch normalizes it     (unified Task model, publishes to message broker)
3. Mastermind plans the work  (selects a Scheme, builds a Dossier with RAG context)
4. Operative executes         (ephemeral container, LLM + Arsenal tools, commits code)
5. Mastermind gates and opens (lint + test gates must pass, then it opens the PR)
6. Forge runs CI on the PR    (lint, tests, silent-failure scan, PR comment)
7. You review the PR          (human-in-the-loop, always)
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
    F --> C
    C --> I["Pull Request\n(ready for review)"]
    I --> H["Forge\n(post-PR CI)"]

    style A fill:#1a1a2e,stroke:#7c3aed,color:#e2e8f0
    style C fill:#1a1a2e,stroke:#7c3aed,color:#e2e8f0
    style F fill:#1a1a2e,stroke:#7c3aed,color:#e2e8f0
    style I fill:#1a1a2e,stroke:#34d399,color:#e2e8f0
```

---

## Components

| Component | Path | Role |
|---|---|---|
| **Mastermind** | `src/henchmen/mastermind/` | Orchestrator. Scheme selection, DAG execution, operative dispatch, stalled-task watchdog. Fail-closed lint/test gates, then opens the PR. |
| **Dispatch** | `src/henchmen/dispatch/` | Intake router. Normalizes tasks from all sources into a unified Task model. |
| **Operative** | `src/henchmen/operative/` | Coding agent. Ephemeral container. Executes scheme nodes with Arsenal tools. |
| **Arsenal** | `src/henchmen/arsenal/` | Tool registry, in-process inside the Operative. `code_edit`, `code_intel`, `context`, `git_ops`, `github`, `jira`, `slack`, `test_runner`. |
| **Forge** | `src/henchmen/forge/` | Post-PR CI. Runs lint/tests on the PR Mastermind opened, detects silent failures, comments the result (passed, failed, or incomplete when a check could not run). |
| **Dossier** | `src/henchmen/dossier/` | Context builder. Rules, semantic code search via Vertex AI RAG Engine, task analysis. Uploads the dossier to the object store. |
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
pip install -e ".[local]"            # Anthropic + OpenAI + Slack + evals
pip install -e ".[local,dev]"        # the above plus pytest/ruff/mypy
pip install -e ".[gcp]"              # Vertex AI, Pub/Sub, Firestore, GCS, Cloud Run
pip install -e ".[aws]"              # Bedrock, SNS/SQS, DynamoDB, S3, ECS
pip install -e ".[all]"              # every backend
pip install -e ".[dev-integration]"  # everything, for the integration suite
```

`dev` is tooling only. Layer it on whichever runtime extra you need.

---

## Configuration

`henchmen init` writes `.env.local` for you. To edit by hand, copy the template:

```bash
cp .env.example .env.local
```

Every setting uses the `HENCHMEN_` prefix. The credential settings also accept
the bare names a Cloud Run secret mount injects (`GITHUB_TOKEN`,
`SLACK_BOT_TOKEN`, ...), with the prefixed name winning when both are set.

| Variable | Description | Default |
|---|---|---|
| `HENCHMEN_PROVIDER` | `local`, `gcp`, or `aws` | `gcp` |
| `HENCHMEN_ENVIRONMENT` | `dev`, `staging`, or `prod` | `dev` |
| `HENCHMEN_LLM_PROVIDER` | `anthropic`, `openai`, `local`, `gcp`, `aws` | follows `HENCHMEN_PROVIDER` |
| `HENCHMEN_GITHUB_TOKEN` | Classic PAT with the `repo` scope (used when no GitHub App is configured) | *(required for PRs)* |
| `HENCHMEN_GITHUB_DEFAULT_REPO` | Target repository, `owner/repo` | *(required)* |
| `HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD` | Spend allowed per task | `6.0` |
| `HENCHMEN_DISPATCH_API_TOKEN` | Bearer token for `POST /api/v1/tasks` (open in dev when empty, 401 in staging/prod) | *(empty)* |

See [`.env.example`](.env.example) for every setting with commentary, or
[`src/henchmen/config/settings.py`](src/henchmen/config/settings.py) for the
authoritative list.

### Model tiers

Scheme nodes name a tier, never a model. The configured LLM provider resolves
it, so the same scheme runs unchanged on any provider.

| Tier | Used by | Anthropic | OpenAI | Vertex AI |
|---|---|---|---|---|
| `default/complex` | `implement_fix`, `implement_feature` | `claude-sonnet-5` | `gpt-4.1` | `gemini-2.5-pro` |
| `default/light` | planning, classification | `claude-haiku-4-5` | `gpt-4.1-mini` | `gemini-2.5-flash` |
| `default/reasoning` | `fix_tests`, `analyze_goal` | `claude-opus-5` | `o3` | `gemini-3.1-pro` |

Override any cell with the matching setting, for example
`HENCHMEN_ANTHROPIC_MODEL_COMPLEX` or `HENCHMEN_VERTEX_AI_MODEL_REASONING`.
On Ollama each tier falls back to `HENCHMEN_LLM_OLLAMA_MODEL` unless you set
`HENCHMEN_LLM_OLLAMA_MODEL_COMPLEX` and friends.

---

<details>
<summary><strong>GitHub setup</strong></summary>

Henchmen pushes branches and opens pull requests one of two ways. The
guided-setup Console's GitHub step does the first for you (it creates a
GitHub App from a manifest and walks you through installing it); a **classic
personal access token** works too, and is what a hand-edited `.env.local`
uses.

**GitHub App (created by the Console).** Henchmen then mints short-lived,
repository-scoped installation tokens on demand — operatives never see a
long-lived credential. The App requests contents, pull requests and issues
write plus metadata, checks and actions read, and deliberately never
`workflows`, so a push touching `.github/workflows/` is refused by GitHub and
the task escalates with a clear message instead of failing silently. See
`.env.example` for the `HENCHMEN_GITHUB_APP_*` settings the Console writes; a
partly configured App fails closed rather than falling back to a token.

**Classic personal access token.**

1. Create one at **Settings > Developer settings > Personal access tokens >
   Tokens (classic)** with the **`repo`** scope.
2. Use an account that can push to the target repository. A token without push
   access reaches the repo but cannot open a PR, and `henchmen doctor` says so.
3. Set it:

```bash
HENCHMEN_GITHUB_TOKEN=ghp_your_token
HENCHMEN_GITHUB_DEFAULT_REPO=your-org/your-repo
```

To receive GitHub webhooks (issues labelled `henchmen`, `@henchmen` PR
comments, CI-failure events), point the repo's webhook at
`https://your-dispatch-url/webhooks/github` and set
`HENCHMEN_GITHUB_WEBHOOK_SECRET` to the same secret. Only comments from users
with a trusted association (owner, member, collaborator) can start a run.

</details>

<details>
<summary><strong>Slack setup</strong></summary>

The Slack bot runs inside the Dispatch process over Socket Mode, so it needs
no public URL. When someone @mentions it, Dispatch turns the message into a
task; status updates come back in the same thread.

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps),
   **From scratch**.
2. Enable **Socket Mode** and generate an **App-Level Token** with
   `connections:write`.
3. Under **OAuth & Permissions**, add these **Bot Token Scopes**:
   - `app_mentions:read` — receive @mentions
   - `chat:write` — post status updates
   - `channels:history` — read thread context for richer task descriptions
   - `channels:read`, `groups:read` — list channels during `henchmen init`
   - `channels:join` — join the notification channel on startup
4. Under **Event Subscriptions**, subscribe to the bot event `app_mention`.
5. Install the app to your workspace.
6. Run `henchmen init --section slack`, or set them yourself:

```bash
HENCHMEN_SLACK_BOT_TOKEN=xoxb-your-bot-token
HENCHMEN_SLACK_APP_TOKEN=xapp-your-app-level-token
HENCHMEN_SLACK_SIGNING_SECRET=your-signing-secret
HENCHMEN_SLACK_NOTIFICATION_CHANNEL=C0123CHANNEL
```

The bot joins `HENCHMEN_SLACK_NOTIFICATION_CHANNEL` when it starts. Private
channels cannot be self-joined — invite the bot with `/invite @YourBot`.

</details>

<details>
<summary><strong>Jira setup</strong></summary>

1. In your Jira project, go to **Settings > Webhooks > Create Webhook**.
2. Point it at `https://your-dispatch-url/webhooks/jira`.
3. Select the issue events you want to trigger work.
4. Create an API token at
   [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens)
   and set:

```bash
HENCHMEN_JIRA_BASE_URL=https://your-org.atlassian.net
HENCHMEN_JIRA_EMAIL=your-service-account@your-org.com
HENCHMEN_JIRA_API_TOKEN=your-jira-api-token
HENCHMEN_JIRA_WEBHOOK_SECRET=shared-secret
HENCHMEN_JIRA_REPO_FIELD=customfield_10042
HENCHMEN_JIRA_BRANCH_FIELD=customfield_10043
```

`HENCHMEN_JIRA_WEBHOOK_SECRET` verifies the webhook signature. It is required in
staging and prod — without it every Jira delivery is rejected with 401. The
operative's Jira tools use the base URL, email and API token.

A Jira webhook delivers custom fields only under their numeric id
(`customfield_<number>`), never under a name like "Repository". If your issues
carry the target repository (`owner/repo`) and branch in custom fields, set
`HENCHMEN_JIRA_REPO_FIELD` and `HENCHMEN_JIRA_BRANCH_FIELD` to those ids. To
find an id, open **Jira settings > Issues > Custom fields**, choose the field's
**...** menu and read the numeric id from the page URL (`10042` becomes
`customfield_10042`), or call
`GET https://your-org.atlassian.net/rest/api/3/field` and take the `"id"` of the
field whose `"name"` matches. An issue with no repository field falls back to
`HENCHMEN_GITHUB_DEFAULT_REPO`.

</details>

---

## Development

```bash
ruff check --fix src/ tests/ evals/   # Auto-fix lint
ruff check src/ tests/ evals/          # Verify clean
ruff format src/ tests/ evals/         # Format
mypy src/ evals/                       # Type check
pytest tests/unit/                     # Unit tests
```

All five must pass before submitting a PR. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Troubleshooting

Start with `henchmen doctor`. It builds the same `Settings` the services use,
so it sees your `.env.local`, and it probes each configured credential against
the real API. `henchmen doctor --offline` skips the network calls.
`henchmen config` prints the effective settings with credentials masked;
`--only-set` limits it to the values you changed from the defaults — the
quickest way to see whether `.env.local` or an exported variable won.

**`henchmen-operative:local` image build fails**
Check that Docker Desktop is running with at least 4GB of RAM. Try
`henchmen build-operative --no-cache`. The first build takes 5–10 minutes on a
slow connection.

**Task dispatched but nothing happens**
Check the server terminal. Verify Docker is running with `docker ps`, and that
`HENCHMEN_GITHUB_TOKEN` is set and has the `repo` scope (classic PAT, not
fine-grained).

**`401 Unauthorized` when creating a PR**
The token is missing the `repo` scope or has expired. `henchmen doctor` reports
both, including whether the token can actually push to your default repo.

**Operative runs but the PR is empty or touches the wrong files**
Usually an LLM tool-calling failure. Ollama models below about 14B routinely
fail the multi-step tool loop; switch to Anthropic or OpenAI, or pull a larger
model.

**`Connection refused` to Ollama**
The operative container reaches the host at `http://host.docker.internal:11434`.
Confirm `ollama serve` is running: `curl http://localhost:11434/api/tags`.

**Linux: `permission denied` on the Docker socket**
`sudo usermod -aG docker $USER`, then log out and back in.

**Every local lint/test gate fails with "uid 65534 is not mapped in this Docker user namespace"**
The gate container runs repository code as uid 65534 (`nobody`). Rootless Docker and `userns-remap`
must map that uid: add a subordinate uid range that covers it in `/etc/subuid` and `/etc/subgid`, or run
the gates on a Docker engine without user-namespace remapping.

See [docs/troubleshooting.md](docs/troubleshooting.md) for the full guide.

---

## Supported Languages

Henchmen detects the target repository's stack at runtime via manifest
files and runs the appropriate lint / test commands. The following
stacks are detected and supported out of the box:

| Stack        | Detected by                                      | Test command                          | Lint command                     |
|--------------|--------------------------------------------------|---------------------------------------|----------------------------------|
| Python       | `pyproject.toml`, `setup.py`, `requirements.txt` | `python -m pytest -q`                 | `python -m ruff check .`         |
| Rust         | `Cargo.toml`                                     | `cargo test`                          | `cargo clippy -- -D warnings`    |
| Go           | `go.mod`                                         | `go test ./...`                       | `go vet ./...`                   |
| Java (Maven) | `pom.xml`                                        | `mvn test`                            | `mvn verify -DskipTests`         |
| Java (Gradle)| `build.gradle` or `build.gradle.kts`             | `./gradlew test`                      | `./gradlew check -x test`        |
| Node (pnpm)  | `package.json` + `pnpm-lock.yaml`                | `pnpm run --if-present test`          | `pnpm run --if-present lint`     |
| Node (npm)   | `package.json` (no pnpm lockfile)                | `npm run --if-present test`           | `npm run --if-present lint`      |

The lint column is the stack's project-wide command. The Mastermind lint gate
narrows it to the files the operative changed against the base branch: `ruff`
on changed Python files, `eslint` on changed JS/TS files (from the nearest
`package.json`), `go vet` on changed Go packages, and the Rust or Java command
only when the branch touched that language. If that diff cannot be computed
the gate fails.

Detection runs top to bottom and the first match wins, so a repo with both
`pyproject.toml` and `package.json` is treated as Python. If no manifest
matches, the lint and test gates **fail** and the task escalates for human
review — Henchmen will not open a PR it could not check. See
`src/henchmen/utils/stack_detector.py` for the detection logic and add a new
stack via a pull request.

---

## Reproducibility: verify BYO-LLM parity yourself

Henchmen supports 5 LLM providers (Vertex AI Gemini, AWS Bedrock,
OpenAI, Anthropic, and Ollama). To measure how close your chosen
provider gets to the Gemini 2.5 Pro baseline on your own hardware /
account, run the eval harness:

```bash
# Run a single fixture against the provider of your choice.
henchmen eval run --provider openai --fixture bugfix_off_by_one

# Run every fixture in evals/fixtures/ (11 ship today — add your own there).
henchmen eval run --provider ollama

# Record this run as the provider's baseline.
henchmen eval run --provider openai --write-baseline
```

Every run is saved to a local SQLite history (`henchmen eval history`,
`henchmen eval compare <run_a> <run_b>`). `--write-baseline` also writes the
provider's entry in `evals/baseline.json`; `--compare-baseline` fails on a
drop of more than 5%. If you want to publish your numbers, open a PR
updating `evals/baseline.json`. See [`evals/README.md`](evals/README.md).

The [`.github/workflows/evals.yml`](.github/workflows/evals.yml)
workflow is `workflow_dispatch`-triggered so you can run it against
your own GitHub Actions runner with your own secrets — it opens a PR
updating `evals/baseline.json` for review.

---

## Documentation

- [Architecture](docs/architecture.md)
- [Schemes](docs/schemes.md)
- [Cost Model](docs/cost-model.md)
- [Deploying on GCP](docs/deploy-gcp.md)
- [Operations](docs/operations.md)
- [Incident Runbook](docs/incident-runbook.md)
- [Rollback Procedures](docs/rollback-procedures.md)
- [Troubleshooting](docs/troubleshooting.md)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for code style, PR process, and how to add new providers.

## License

[Apache 2.0](LICENSE)
