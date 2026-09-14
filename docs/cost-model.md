# Cost Model and Optimization

## Model Pricing Table

Prices are per 1 million tokens and come from `PRICE_TABLE` in
`src/henchmen/providers/pricing.py`, the one place Henchmen prices tokens.
They are list prices captured at the time of writing — check each vendor's
pricing page for current rates. The rows below are the default model for each
tier; `PRICE_TABLE` also prices older and smaller models in each family.

| Tier | Model | Input ($/1M) | Output ($/1M) |
|------|-------|--------------|---------------|
| `default/reasoning` | `gemini-3.1-pro` | 2.00 | 12.00 |
| `default/complex` | `gemini-2.5-pro` | 1.25 | 10.00 |
| `default/light` | `gemini-2.5-flash` | 0.30 | 2.50 |
| `default/reasoning` | `claude-opus-5` | 5.00 | 25.00 |
| `default/complex` | `claude-sonnet-5` | 2.00 | 10.00 |
| `default/light` | `claude-haiku-4-5` | 1.00 | 5.00 |
| `default/reasoning` | `o3` | 2.00 | 8.00 |
| `default/complex` | `gpt-4.1` | 2.00 | 8.00 |
| `default/light` | `gpt-4.1-mini` | 0.40 | 1.60 |

Cached input is billed at a discount the table encodes per vendor: 10% of the
input rate for Anthropic prompt caching (cache writes at 125%), 25% for Gemini
and OpenAI cached tokens. Cost is always computed with `estimate_cost` /
`estimate_cost_for_settings` — never a second table. A scheme node stores a
tier name, so cost is computed after resolving the tier to the active
provider's concrete model; a model missing from `PRICE_TABLE` costs `$0.00`
and never trips the cost ceiling (see `docs/incident-runbook.md`).

## Per-Task Cost Breakdown by Node

The estimates below use the **Vertex AI defaults** (`gemini-2.5-pro` for
`default/complex`, `gemini-3.1-pro` for `default/reasoning`). Multiply by the
ratio of rates in the table above for other providers. Deterministic nodes —
including `verify_changes` and `fix_lint` — never call a model and cost $0.00.

### bugfix_standard

A typical bugfix task makes all its model calls in one agentic node:

| Node | Tier (Vertex model) | Steps | Est. Input Tokens | Est. Output Tokens | Est. Cost |
|------|---------------------|-------|-------------------|--------------------|-----------|
| `implement_fix` | `default/complex` (Gemini 2.5 Pro) | ~15-25 | ~150K-300K | ~10K-30K | $0.29-$0.68 |
| Deterministic nodes | -- | -- | -- | -- | $0.00 |
| **Total** | | | | | **$0.29-$0.68** |

If tests fail and `fix_tests` is invoked:

| Node | Tier (Vertex model) | Steps | Est. Input Tokens | Est. Output Tokens | Est. Cost |
|------|---------------------|-------|-------------------|--------------------|-----------|
| `fix_tests` | `default/reasoning` (Gemini 3.1 Pro) | ~5-10 | ~50K-100K | ~5K-15K | $0.16-$0.38 |

**Total with test fix retry:** $0.45-$1.06

### feature_standard

Feature tasks use the same pipeline as bugfix, with `implement_feature` in place of `implement_fix` and a larger step budget:

| Node | Tier (Vertex model) | Steps | Est. Input Tokens | Est. Output Tokens | Est. Cost |
|------|---------------------|-------|-------------------|--------------------|-----------|
| `implement_feature` | `default/complex` (Gemini 2.5 Pro) | ~20-35 | ~200K-500K | ~15K-50K | $0.40-$1.13 |
| **Total** | | | | | **$0.40-$1.13** |

Add the `fix_tests` row above if the test gate fails.

### goal_decomposition

The cheapest scheme -- planning only, no code changes:

| Node | Tier (Vertex model) | Steps | Est. Input Tokens | Est. Output Tokens | Est. Cost |
|------|---------------------|-------|-------------------|--------------------|-----------|
| `analyze_goal` | `default/reasoning` (Gemini 3.1 Pro) | ~3-5 | ~30K-60K | ~3K-8K | $0.10-$0.22 |
| **Total** | | | | | **$0.10-$0.22** |

### CI Failure Auto-Fix

When CI fails on a Henchmen PR, the auto-fix loop dispatches a fix operative:

| Node | Tier (Vertex model) | Steps | Est. Input Tokens | Est. Output Tokens | Est. Cost |
|------|---------------------|-------|-------------------|--------------------|-----------|
| `implement_fix` (CI fix) | `default/complex` (Gemini 2.5 Pro) | ~10-20 | ~100K-200K | ~5K-20K | $0.18-$0.45 |

Max 2 attempts, so worst case: $0.36-$0.90 for CI auto-fix.

### Cost Ceiling

`HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD` (default `6.0`) caps the cumulative
spend of one task across every node. The scheme executor checks it before
dispatching each agentic node, and the operative enforces it inside its loop;
either way the node fails closed and the task escalates.

## Infrastructure Costs

These are ongoing GCP costs independent of task volume:

| Resource | Dev Cost | Prod Cost | Notes |
|----------|----------|-----------|-------|
| Cloud Run Services (3) | ~$0/mo (scale-to-zero) | ~$50-100/mo (min 1 instance) | Mastermind at 2 vCPU/4Gi is the largest |
| Cloud Run Jobs (Lairs) | Per-task only | Per-task only | Sized by `lair_cpu` / `lair_memory` (dev 2 vCPU/4Gi), billed per second |
| Pub/Sub | ~$1-5/mo | ~$5-20/mo | 7 topics, push subscriptions |
| Firestore | ~$0-5/mo | ~$5-20/mo | `task_executions`, `operative_reports`, `processed_messages` collections |
| Cloud Storage | ~$1/mo | ~$1-5/mo | Dossier artifacts, Terraform state |
| Secret Manager | ~$1/mo | ~$1/mo | 7 secrets |
| Artifact Registry | ~$1-5/mo | ~$1-5/mo | Docker images |
| VPC Connector | ~$7/mo | ~$7/mo | Serverless VPC access |

**Estimated monthly infrastructure (dev):** $10-25/mo (excluding LLM costs)
**Estimated monthly infrastructure (prod):** $75-160/mo (excluding LLM costs)

## Cost Optimization Strategies

### 1. Model Tiering (Implemented)

The most impactful optimization. Each node names the cheapest tier that can
handle it, and the configured provider resolves the tier to a concrete model
(see `docs/schemes.md`). **Hard rule:** on Vertex AI only Gemini models are
used -- no Claude on Vertex AI.

| Work | Tier | Why |
|------|------|-----|
| Core fix/feature (`implement_fix`, `implement_feature`) | `default/complex` | Best cost/quality for code generation |
| Test fixes and goal decomposition (`fix_tests`, `analyze_goal`) | `default/reasoning` | Diagnosis-heavy, short, and worth the higher rate |
| Planning, classification, RAG reranking, `henchmen chat` | `default/light` | High volume, latency-sensitive, low difficulty |
| Verification and lint fixing (`verify_changes`, `fix_lint`) | none (deterministic) | No model call at all |

On Vertex AI, running verification-style work on `gemini-2.5-flash` instead of
`gemini-3.1-pro` cuts input cost by 85% and output cost by ~80%; keeping core
coding on `gemini-2.5-pro` instead of `gemini-3.1-pro` saves ~38% on input and
~17% on output.

### 2. Deterministic Gates (Implemented)

`fix_lint` runs the linter's own fixer (`ruff check --fix`, `npx eslint . --fix`,
or `pnpm run lint:fix` in a turbo monorepo) and `verify_changes` checks the
branch with git — neither calls an LLM. Auto-fixers handle the majority of lint
issues (whitespace, import ordering, trailing commas) deterministically.

**Cost without this optimization:** An agentic lint-fix node on the reasoning tier would cost ~$0.15-$0.30 per invocation.
**Cost with this optimization:** $0.00.

### 3. Lint Scope

Forge's post-PR lint runs `ruff check` only on the Python files the PR changed,
so pre-existing violations elsewhere do not fail it. The Mastermind lint gate
(`run_lint` / `run_lint_retry`) currently runs the detected stack's lint command
over the whole workspace (for example `python -m ruff check .` or
`npm run --if-present lint`), so a target repository with pre-existing lint
failures fails the gate, runs `fix_lint`, and can escalate on code the
operative never touched. Keep the target repository lint-clean, or expect
those escalations.

### 4. Tool Result Truncation (Implemented)

Tool results over 10,000 characters are truncated before being added to the
conversation context, and any single message over 64,000 characters is
trimmed. This prevents context window blowup that would increase input token
costs on subsequent model calls.

### 5. Pre-Read File Context (Implemented)

The operative pre-reads up to 5 of the most relevant files (4,000 characters
each, scored by task analysis, RAG results, and keyword matching) before the
agent loop begins. This front-loads useful context, reducing the number of
`file_read` tool calls the agent needs (each of which adds a model call cycle).

**Estimated savings:** 2-5 fewer model call rounds per task, saving $0.05-$0.15 at Gemini 2.5 Pro rates.

### 6. Phase-Aware Nudging (Implemented)

The agent loop tracks consecutive read-only steps and nudges the model to start editing when it has spent too many steps exploring. This prevents the common failure mode where an LLM exhausts its step budget reading files without making any changes.

**Estimated savings:** Prevents wasted $0.50-$1.50 on tasks that would have timed out without producing changes.

### 7. Context Windowing (Implemented)

Before each model call, the operative's guardrails keep the seeded preamble
(system prompt, dossier context and task) plus the last 16 messages and drop
the middle of the conversation, preserving tool-call/tool-result pairs. This
bounds input tokens on long sessions.

**Risk:** Dropped context may cause the agent to re-read files it already explored.

### 8. Prompt Caching (Implemented for Anthropic; Gemini and OpenAI cached tokens are priced)

The system prompt and tool definitions are identical across every model call in
one operative run.

- **Anthropic:** the provider marks the system prompt and the tool list with
  `cache_control: ephemeral`, so calls after the first read that prefix from
  cache at 10% of the input rate.
- **Vertex AI (Gemini) and OpenAI:** Henchmen does not create explicit caches,
  but when the API reports cached input tokens (Vertex
  `cached_content_token_count`, OpenAI `cached_tokens`) they are priced at the
  25% cached rate. `HENCHMEN_VERTEX_AI_CONTEXT_CACHE_ENABLED` and
  `HENCHMEN_VERTEX_AI_CONTEXT_CACHE_MIN_TOKENS` are defined in Settings but
  nothing reads them yet.

## Long-Context Pricing

### The >200K Token Cliff

Gemini models on Vertex AI may apply higher pricing for long-context requests (>200K tokens). As the operative conversation grows through tool call cycles, input tokens accumulate:

- Steps 1-10: ~50K-100K input tokens (within standard pricing)
- Steps 10-20: ~100K-200K input tokens (approaching the boundary)
- Steps 20-40: ~200K-500K input tokens (long-context pricing may apply)

**Impact:** A 40-step Gemini 2.5 Pro session with 300K average input tokens costs ~$0.40 in input alone. With extended context pricing, this could increase further. Check Google's pricing page for current long-context multipliers.

**Mitigations:** context windowing (strategy 7), cached-prefix pricing
(strategy 8), and tighter step budgets on agentic nodes.

## Future Optimizations

### Explicit Gemini Context Caches

Creating a Vertex AI `cachedContents` resource for the system instruction and
dossier context would guarantee the cached rate on steps 2+ instead of relying
on implicit caching.

**Estimated savings per task:** $0.10-$0.40 for a typical bugfix.

### Batch API

Vertex AI and other vendors offer batch prediction at reduced rates for non-urgent requests. Candidates are non-interactive light-tier calls such as offline evaluation runs.

**Trade-off:** Batch API adds latency (minutes to hours), which would increase total wall clock time.

### Embedding Model Cost

The RAG pipeline (Vertex AI RAG Engine, corpus: `henchmen-code`) uses embeddings to index repository code. The embedding cost is paid per indexing run, not per task: a `henchmen embed <owner/repo> --full` run embeds every file, while the incremental runs Mastermind performs on each default-branch push (and `henchmen embed` without `--full`) embed only the files changed since the last indexed commit. Embedding and vector storage costs are billed through Vertex AI.

## Cost Tracking

### Real-Time Tracking

Every agentic node reports telemetry via `OperativeReport`:
- `total_input_tokens`, `total_output_tokens`
- `model_calls` (number of LLM API calls)
- `tool_calls_count`, `tool_calls_detail` (breakdown by tool name)
- `wall_clock_seconds`

The `TaskTracker` computes USD cost with `estimate_cost_for_settings()` (tier resolved to the concrete model, then priced from `PRICE_TABLE`) and persists it to the document store with per-node granularity.

### Metrics API

Query aggregated cost data (the bearer token is required in staging and prod):

```bash
# Summary for the last 7 days
curl -H "Authorization: Bearer $HENCHMEN_METRICS_AUTH_TOKEN" "https://mastermind-url/metrics/summary?days=7"
```

Response includes:
- `total_cost_usd`: Total spend across all tasks
- `avg_cost_per_task_usd`: Average cost per task
- `by_scheme`: Count, CI pass rate and average cost per scheme
- `total_input_tokens`, `total_output_tokens`: Raw token counts
- `ci_pass_rate`: Quality metric (higher = fewer wasted fix cycles; `null` when no CI result has landed)

### Firestore Cost Records

Each task document in `task_executions` contains:
- `estimated_cost_usd`: Total estimated cost for the task
- `node_metrics.{node_id}.cost_usd`: Per-node cost breakdown

Example Firestore query for high-cost tasks:
```python
db.collection("task_executions")
  .where("estimated_cost_usd", ">=", 2.0)
  .order_by("estimated_cost_usd", direction="DESCENDING")
  .limit(10)
```
