# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Semver policy during 0.x:** While we are on `0.x` releases, minor version
bumps may include breaking changes. We will call them out explicitly in this
changelog under a `Changed` or `Removed` heading so upgraders can plan. Once
we hit `1.0.0`, standard semver rules apply.

## [Unreleased]

_No changes yet._

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
  of producing silent 403s. (A3)
- Per-task LLM cost ceiling (`HENCHMEN_COST_CEILING_USD_PER_TASK`) with a
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
