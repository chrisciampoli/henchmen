# Cost Model and Optimization

## Model Pricing Table

Prices are per 1 million tokens, sourced from the `_PRICE_MAP` in `src/henchmen/observability/tracker.py`. These are placeholders -- check Google's Vertex AI pricing page for current rates.

| Model | Input ($/1M tokens) | Output ($/1M tokens) | Used For |
|-------|---------------------|----------------------|----------|
| Gemini 3.1 Pro (`gemini-3.1-pro`) | ~$2.00 | ~$12.00 | `fix_tests` |
| Gemini 2.5 Pro (`gemini-2.5-pro`) | ~$1.25 | ~$10.00 | `implement_fix`, `implement_feature`, `vertex_ai_model_complex` |
| Gemini 2.5 Flash (`gemini-2.5-flash`) | ~$0.075 | ~$0.30 | `verify_changes`, `plan_implementation` |

## Per-Task Cost Breakdown by Node

### bugfix_standard

A typical bugfix task uses 3 model calls across 2 agentic nodes:

| Node | Model | Steps | Est. Input Tokens | Est. Output Tokens | Est. Cost |
|------|-------|-------|-------------------|--------------------|-----------|
| `implement_fix` | Gemini 2.5 Pro | ~15-25 | ~150K-300K | ~10K-30K | $0.29-$0.68 |
| `verify_changes` | Gemini 2.5 Flash | ~3-5 | ~20K-50K | ~1K-3K | $0.002-$0.005 |
| Deterministic nodes | -- | -- | -- | -- | $0.00 |
| **Total** | | | | | **$0.29-$0.68** |

If tests fail and `fix_tests` is invoked:

| Node | Model | Steps | Est. Input Tokens | Est. Output Tokens | Est. Cost |
|------|-------|-------|-------------------|--------------------|-----------|
| `fix_tests` | Gemini 3.1 Pro | ~5-10 | ~50K-100K | ~5K-15K | $0.16-$0.38 |

**Total with test fix retry:** $0.45-$1.06

### feature_standard

Feature tasks use the same structure as bugfix plus a planning step:

| Node | Model | Steps | Est. Input Tokens | Est. Output Tokens | Est. Cost |
|------|-------|-------|-------------------|--------------------|-----------|
| `plan_implementation` | Gemini 2.5 Flash | ~5-8 | ~30K-80K | ~2K-5K | $0.003-$0.008 |
| `implement_feature` | Gemini 2.5 Pro | ~20-35 | ~200K-500K | ~15K-50K | $0.40-$1.13 |
| `verify_changes` | Gemini 2.5 Flash | ~3-5 | ~20K-50K | ~1K-3K | $0.002-$0.005 |
| **Total** | | | | | **$0.41-$1.14** |

### goal_decomposition

The cheapest scheme -- planning only, no code changes:

| Node | Model | Steps | Est. Input Tokens | Est. Output Tokens | Est. Cost |
|------|-------|-------|-------------------|--------------------|-----------|
| `analyze_goal` | Gemini 3.1 Pro | ~3-5 | ~30K-60K | ~3K-8K | $0.10-$0.22 |
| **Total** | | | | | **$0.10-$0.22** |

### CI Failure Auto-Fix

When CI fails on a Henchmen PR, the auto-fix loop dispatches a fix operative:

| Node | Model | Steps | Est. Input Tokens | Est. Output Tokens | Est. Cost |
|------|-------|-------|-------------------|--------------------|-----------|
| `implement_fix` (CI fix) | Gemini 2.5 Pro | ~10-20 | ~100K-200K | ~5K-20K | $0.18-$0.45 |

Max 2 retries, so worst case: $0.36-$0.90 for CI auto-fix.

## Infrastructure Costs

These are ongoing GCP costs independent of task volume:

| Resource | Dev Cost | Prod Cost | Notes |
|----------|----------|-----------|-------|
| Cloud Run Services (4) | ~$0/mo (scale-to-zero) | ~$50-100/mo (min 1 instance) | Mastermind at 2 vCPU/4Gi is the largest |
| Cloud Run Jobs (Lairs) | Per-task only | Per-task only | 4 vCPU/8Gi, billed per second |
| Pub/Sub | ~$1-5/mo | ~$5-20/mo | 10 topics, push subscriptions |
| Firestore | ~$0-5/mo | ~$5-20/mo | task_executions, merge_queue collections |
| Cloud Storage | ~$1/mo | ~$1-5/mo | Dossier artifacts, snapshots |
| Secret Manager | ~$1/mo | ~$1/mo | 5-6 secrets |
| Artifact Registry | ~$1-5/mo | ~$1-5/mo | Docker images |
| VPC Connector | ~$7/mo | ~$7/mo | Serverless VPC access |

**Estimated monthly infrastructure (dev):** $10-25/mo (excluding LLM costs)
**Estimated monthly infrastructure (prod):** $75-160/mo (excluding LLM costs)

## Cost Optimization Strategies

### 1. Model Tiering (Implemented)

The most impactful optimization. Each node uses the cheapest Gemini model that can handle its task. **Hard rule:** no Claude models on Vertex AI -- Gemini only.

| Task | Expensive Approach | Henchmen Approach | Savings |
|------|-------------------|-------------------|---------|
| Verify changes | Gemini 3.1 Pro (~$0.25) | Gemini 2.5 Flash (~$0.005) | 98% |
| Plan implementation | Gemini 3.1 Pro (~$0.25) | Gemini 2.5 Flash (~$0.005) | 98% |
| Fix test failures | Gemini 2.5 Pro (fallback) | Gemini 3.1 Pro | reasoning uplift |
| Core fix/feature | Gemini 3.1 Pro (~$0.90) | Gemini 2.5 Pro (~$0.50) | 44% |

Tiering keeps the expensive reasoning model (Gemini 3.1 Pro) reserved for the one node that needs it (`fix_tests`) while everything else uses the cheaper Gemini 2.5 Pro or Flash tiers.

### 2. Deterministic fix_lint (Implemented)

The `fix_lint` node runs `eslint --fix` or `ruff --fix` directly -- no LLM call needed. Auto-fixers handle the majority of lint issues (whitespace, import ordering, trailing commas, etc.) deterministically.

**Cost without this optimization:** An agentic lint fix node using Gemini 3.1 Pro would cost ~$0.15-$0.30 per invocation.
**Cost with this optimization:** $0.00 (no LLM call).

### 3. Lint-Only-Changed-Files (Implemented)

The `run_lint` and `run_lint_retry` deterministic nodes only lint files that the operative actually changed (using `git diff --name-only origin/main`). This avoids failing on pre-existing lint warnings in the target repository.

Without this optimization, lint failures from code the operative never touched would trigger unnecessary fix cycles, adding cost.

### 4. Tool Result Truncation (Implemented)

Large tool results (>30K characters) are truncated before being added to the conversation context. This prevents context window blowup that would increase input token costs on subsequent model calls. The truncation is logged so the user can investigate if needed.

### 5. Pre-Read File Context (Implemented)

The operative pre-reads the 10 most relevant files (scored by task analysis, RAG results, and keyword matching) before the agent loop begins. This front-loads useful context, reducing the number of `file_read` tool calls the agent needs (each of which adds a model call cycle).

**Estimated savings:** 2-5 fewer model call rounds per task, saving $0.05-$0.15 at Gemini 2.5 Pro rates.

### 6. Phase-Aware Nudging (Implemented)

The agent loop tracks consecutive read-only steps and nudges the model to start editing when it has spent too many steps exploring. This prevents the common failure mode where an LLM exhausts its step budget reading files without making any changes.

**Estimated savings:** Prevents wasted $0.50-$1.50 on tasks that would have timed out without producing changes.

## Long-Context Pricing

### The >200K Token Cliff

Gemini models on Vertex AI may apply higher pricing for long-context requests (>200K tokens). As the operative conversation grows through tool call cycles, input tokens accumulate:

- Steps 1-10: ~50K-100K input tokens (within standard pricing)
- Steps 10-20: ~100K-200K input tokens (approaching the boundary)
- Steps 20-40: ~200K-500K input tokens (long-context pricing may apply)

**Impact:** A 40-step Gemini 2.5 Pro session with 300K average input tokens costs ~$0.40 in input alone. With extended context pricing, this could increase further. Check Google's pricing page for current long-context multipliers.

**Mitigation strategies (not yet implemented):**
- Context windowing (see below)
- Prompt caching (see below)
- Reducing max_steps and improving prompt efficiency

## Future Optimizations

### Prompt Caching

Gemini on Vertex AI supports context caching, where repeated prefixes are charged at reduced rates.

**Opportunity:** The system prompt + dossier context is identical across all model calls within a single operative run. Caching this prefix would reduce input token costs by 60-80% for steps 2+.

**Estimated savings per task:** $0.10-$0.40 for a typical bugfix.

**Implementation:** Use the Gemini `cached_content` API to mark the system instruction and dossier context as cacheable.

### Batch API

Vertex AI offers batch prediction with cost reductions for non-urgent requests.

**Opportunity:** The `verify_changes` and `plan_implementation` nodes are not latency-sensitive. Running them via batch API would halve their already-low costs.

**Estimated savings per task:** $0.005 (minimal, since these nodes already use the cheapest model).

**Trade-off:** Batch API adds latency (minutes to hours), which would increase total wall clock time.

### Context Windowing

As the conversation grows, older tool results become less relevant. A sliding window strategy would:

1. Keep the system prompt and dossier context (always relevant)
2. Keep the last N tool call/result pairs
3. Summarize or drop older interactions

**Estimated savings per task:** 30-50% reduction in input tokens for long-running sessions (>20 steps), saving $0.15-$0.45.

**Risk:** Dropping context may cause the agent to re-read files it already explored or lose track of its progress.

### Embedding Model Cost

The RAG pipeline (Vertex AI RAG Engine, corpus: `henchmen-code`) uses embeddings to index repository code. The embedding cost is a one-time expense per repository indexing run, not per-task. Embedding and vector storage costs are billed through Vertex AI.

## Cost Tracking

### Real-Time Tracking

Every agentic node reports telemetry via `OperativeReport`:
- `total_input_tokens`, `total_output_tokens`
- `model_calls` (number of LLM API calls)
- `tool_calls_count`, `tool_calls_detail` (breakdown by tool name)
- `wall_clock_seconds`

The `TaskTracker` uses the `estimate_cost()` function to calculate USD cost from token counts and model name, then persists to Firestore with per-node granularity.

### Metrics API

Query aggregated cost data:

```bash
# Summary for the last 7 days
curl https://mastermind-url/metrics/summary?days=7
```

Response includes:
- `total_cost_usd`: Total spend across all tasks
- `avg_cost_per_task_usd`: Average cost per task
- `by_scheme`: Cost breakdown by scheme (bugfix vs feature vs goal_decomposition)
- `total_input_tokens`, `total_output_tokens`: Raw token counts
- `ci_pass_rate`: Quality metric (higher = fewer wasted fix cycles)

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
