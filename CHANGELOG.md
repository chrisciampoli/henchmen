# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Semver policy during 0.x:** While we are on `0.x` releases, minor version
bumps may include breaking changes. We will call them out explicitly in this
changelog under a `Changed` or `Removed` heading so upgraders can plan. Once
we hit `1.0.0`, standard semver rules apply.

## [Unreleased]

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
