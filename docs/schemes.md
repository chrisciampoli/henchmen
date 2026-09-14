# Scheme Reference

## What is a Scheme?

A Scheme is a directed graph that defines the workflow Henchmen follows to complete a task. Each node is either a deterministic step (an inline handler in the Mastermind, no LLM) or an agentic step (an ephemeral container running an LLM agent). Edges connect nodes with optional conditions (`pass`/`fail`) to create branching workflows.

Schemes are defined in `src/henchmen/schemes/` as Python modules. Each module constructs a `SchemeDefinition` (a Pydantic model) and registers it with the `SchemeRegistry` at import time.

## Core Concepts

### SchemeDefinition

Defined in `src/henchmen/models/scheme.py`:

```python
class SchemeDefinition(BaseModel):
    id: str                      # Unique scheme ID (e.g., "bugfix_standard")
    name: str                    # Human-readable name
    description: str             # What this scheme accomplishes
    version: str                 # Semantic version
    nodes: list[SchemeNode]      # All nodes in the workflow
    edges: list[SchemeEdge]      # Directed edges defining flow
```

### SchemeNode

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` | Unique identifier within the scheme (e.g., `implement_fix`). Deterministic nodes are matched to their handler by this id. |
| `name` | `str` | Human-readable name |
| `node_type` | `NodeType` | `DETERMINISTIC` or `AGENTIC` |
| `arsenal_requirement` | `ArsenalRequirement \| None` | Which tool sets the operative can access (`code_edit`, `code_intel`, `context`, `git_ops`, `github`, `jira`, `slack`, `test_runner`) and whether destructive tools are allowed |
| `dossier_requirement` | `DossierRequirement \| None` | What context to pre-fetch |
| `max_steps` | `int` | Max agentic loop iterations (default: 20), used when no step budget applies |
| `step_budget` | `StepBudget \| None` | Adaptive budget (base steps, extensions on progress, hard max). When unset, `STEP_BUDGET_DEFAULTS` supplies one for `implement_fix`, `implement_feature`, `fix_tests` and `analyze_goal`; any other node uses `max_steps`. |
| `timeout_seconds` | `int` | Execution timeout (default: 300) |
| `instruction_template` | `str \| None` | System instruction for the agentic loop. Required on agentic nodes, forbidden on deterministic ones. |
| `model_name` | `str \| None` | Model **tier** (`default/complex`, `default/light`, `default/reasoning`). Required on agentic nodes, forbidden on deterministic ones. |
| `grounding_enabled` | `bool` | Request Google Search grounding (Vertex AI only; default `False`) |

### SchemeEdge

```python
class SchemeEdge(BaseModel):
    from_node: str                           # Source node ID
    to_node: str                             # Destination node ID
    condition: Literal["pass", "fail"] | None  # Edge condition (None = unconditional)
```

### Node Types

**DETERMINISTIC** nodes run inline handlers (`src/henchmen/mastermind/scheme_executor/handlers.py`). They never call an LLM. A deterministic node whose id has no registered handler fails.

| Handler ID | What It Does |
|------------|-------------|
| `create_branch` | Returns the branch name `henchmen/{task_id[:8]}`; the operative bootstrap creates the branch itself |
| `prefetch_context` | Returns the dossier artifact URI |
| `verify_changes` | Clones the branch and fails unless it has commits and changed files ahead of `origin/<base>` |
| `run_lint` / `run_lint_retry` | Clones the branch, detects the stack (`utils/stack_detector.py`), lists the files the branch changed (`git diff --name-only origin/<base>...HEAD`) and lints only those (`scheme_executor/lint_scope.py`): Python runs `ruff check` on the changed `.py` files; Node runs `eslint` on the changed JS/TS files from each file's nearest `package.json` directory (skipping packages with no ESLint dependency or config); Go runs `go vet` on the packages with changed `.go` files; Rust and Java run the stack's whole-project lint, but only when the branch changed files of that language or its build manifest. No relevant changed files passes with a "no changed ... files to lint" message. If the diff against `origin/<base>` cannot be computed the gate fails. In local mode the commands run inside the operative image (`HENCHMEN_OPERATIVE_IMAGE`, default `henchmen-operative:local`). |
| `fix_lint` | Runs `ruff check --fix` on changed `.py` files or `eslint --fix` on changed JS/TS files (from each file's nearest `package.json`), reverts any fix outside those files, then commits and pushes the in-scope changes. Skipped for stacks without an auto-fixer. No LLM. |
| `run_tests` / `run_tests_retry` | Same as `run_lint`, with the stack's test command (e.g. `python -m pytest -q`, `go test ./...`) |
| `create_pr` | Opens a GitHub pull request via the GitHub API; fails if the repo or GitHub token is missing |
| `escalate` | Marks the task for human review |
| `report_plan` | Reports the `analyze_goal` decomposition back to the user |

Every CI handler fails closed: a clone failure, an undetectable stack, a lint diff that cannot be computed or a non-zero exit code returns `fail`.

**AGENTIC** nodes are dispatched to Lairs. The `LairManager` creates a Cloud Run Job (or a Docker container in local mode) from the operative image, injects the runtime environment (task ID, node ID, model tier, repo, branch, ...), and waits for the operative's report.

### Edge Conditions

Edges can be:

- **Unconditional** (`condition=None`): Always followed. Used for linear flow (e.g., `create_branch -> prefetch_context`).
- **Conditional** (`condition="pass"` or `condition="fail"`): Followed based on the node's result. Deterministic nodes return `pass` or `fail` from their checks. Agentic nodes return `pass` if the operative completed successfully, `fail` otherwise.

When a node returns a condition but no matching conditional edge exists, the executor falls back to unconditional edges. If no edges match at all, the node is treated as terminal.

### Fail-Closed Gates

1. **Lint fails -> auto-fix -> re-lint -> escalate:** If lint still fails after `fix_lint`, the task escalates. It does not proceed to PR creation.
2. **Tests fail -> LLM fix -> re-test -> escalate:** If tests fail after the `fix_tests` attempt, the task escalates.
3. **No changes -> escalate:** If the implementation node produced no commits, `verify_changes` fails and the task escalates.
4. **Lair provisioning fails in staging/prod:** The node returns `fail` and the task follows the failure path. Only in dev, and only for implementation nodes, is a provisioning failure simulated as a pass; `fix_lint`/`fix_tests` never simulate.
5. **CI check errors:** If lint or test commands cannot run (clone failure, unknown stack, or for lint a diff against `origin/<base>` that cannot be fetched or computed), the node returns `fail` rather than silently passing.
6. **Lint judges only the operative's changes:** `run_lint` lints the files the branch changed, never pre-existing violations elsewhere in the repository (see the handler table above).
7. **Max node executions:** Each node can run at most 2 times (`SchemeExecutor._max_node_retries`); a third attempt is forced to `fail`.
8. **Cost ceiling:** Before each agentic node the executor checks the task's cumulative cost against `HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD` and fails the node if it would exceed it.

### Retry Loops

The shipped schemes contain no cycles: every retry is an explicit `*_retry` node (`run_lint_retry`, `run_tests_retry`). The validator still permits cycles that include at least one conditional edge, for custom schemes that want a bounded loop; those loops are capped by the 2-execution limit above. Cycles made only of unconditional edges are rejected.

## Current Schemes

`bugfix_standard` and `feature_standard` share the same pipeline, built by `standard_ci_pipeline()` in `src/henchmen/schemes/_shared_templates.py`. They differ only in the implementation node.

### bugfix_standard

**File:** `src/henchmen/schemes/bugfix_standard.py`
**Triggered by:** `task_type: "bugfix"`, or with no `task_type` the keywords "bug", "fix", "error", "crash", "broken" in the title or description (and the default when nothing matches)

```
create_branch
    |
prefetch_context
    |
implement_fix ---------------------- fail --> escalate
    |
verify_changes --------------------- fail --> escalate
    | pass
run_lint --------- fail --> fix_lint --> run_lint_retry -- fail --> escalate
    | pass                                  | pass
    |<--------------------------------------+
run_tests -------- fail --> fix_tests --> run_tests_retry -- fail --> escalate
    | pass                                  | pass
    |<--------------------------------------+
create_pr
```

**Node details:**

| Node | Type | Model tier | Steps (base / max) | Timeout |
|------|------|------------|--------------------|---------|
| `create_branch` | DETERMINISTIC | -- | -- | 30s |
| `prefetch_context` | DETERMINISTIC | -- | -- | 60s |
| `implement_fix` | AGENTIC | `default/complex` | 30 / 50 | 1800s |
| `verify_changes` | DETERMINISTIC | -- | -- | 30s |
| `run_lint` | DETERMINISTIC | -- | -- | 60s |
| `fix_lint` | DETERMINISTIC | -- | -- | 120s |
| `run_lint_retry` | DETERMINISTIC | -- | -- | 60s |
| `run_tests` | DETERMINISTIC | -- | -- | 300s |
| `fix_tests` | AGENTIC | `default/reasoning` | 15 / 25 | 600s |
| `run_tests_retry` | DETERMINISTIC | -- | -- | 300s |
| `create_pr` | DETERMINISTIC | -- | -- | 30s |
| `escalate` | DETERMINISTIC | -- | -- | 30s |

`implement_fix` may use `code_intel`, `code_edit`, `git_ops`, `test_runner` and `context`. `fix_tests` may use `code_edit`, `test_runner` and `code_intel`, and has grounding disabled.

**implement_fix instruction template** (`BUGFIX_INSTRUCTION_TEMPLATE`): locate the relevant code with `grep_search` and `file_read`, form a hypothesis early, apply the minimal fix with `file_edit`, run `type_check` (and optionally `run_lint` / `run_tests`) before committing, then call `git_commit`. Task text is wrapped in `<user_task_input>` tags and treated as data.

### feature_standard

**File:** `src/henchmen/schemes/feature_standard.py`
**Triggered by:** `task_type: "feature"` or `task_type: "refactor"`, or with no `task_type` keywords like "feature", "implement", "build", "create", "add", "new module", "new endpoint", "setup", "scaffold", "portal", "dashboard" — checked after the bugfix keywords, so "Fix crash when adding an item" still routes to `bugfix_standard`

The graph is identical to `bugfix_standard` with `implement_feature` in place of `implement_fix`. There is no separate planning node.

| Node | Type | Model tier | Steps (base / max) | Timeout |
|------|------|------------|--------------------|---------|
| `implement_feature` | AGENTIC | `default/complex` | 50 / 70 | 1800s |

`implement_feature` uses the same tool sets as `implement_fix` and additionally fetches related issues into its dossier. All other nodes match the bugfix table.

### goal_decomposition

**File:** `src/henchmen/schemes/goal_decomposition.py`
**Triggered by:** Keywords in the **title** like "improve", "optimize", "refactor all", "fix all", "update all", "increase coverage", "reduce", "clean up all", "migrate" (checked first, before `task_type`)

This is a lightweight planning-only scheme:

```
analyze_goal -> report_plan
```

| Node | Type | Model tier | Steps (base / max) | Timeout |
|------|------|------------|--------------------|---------|
| `analyze_goal` | AGENTIC | `default/reasoning` | 5 / 10 | 300s |
| `report_plan` | DETERMINISTIC | -- | -- | 30s |

**analyze_goal instruction template:** The operative explores the codebase using read-only `code_intel` tools and produces 3-5 specific, concrete sub-tasks in a structured format:
```
SUBTASK 1: [title]
FILES: [file1.py, file2.py]
CHANGE: [specific description]
```

The plan is reported back to the user (via Slack or other source). It does not execute the sub-tasks -- those would be submitted as separate tasks.

## How to Create a New Scheme

### Step 1: Define the Scheme

Create a new file in `src/henchmen/schemes/`, e.g., `refactor_standard.py`:

```python
"""refactor_standard scheme - safe refactoring with test verification."""

from henchmen.models.llm import ModelTier
from henchmen.models.scheme import (
    ArsenalRequirement,
    DossierRequirement,
    NodeType,
    SchemeDefinition,
    SchemeEdge,
    SchemeNode,
)
from henchmen.schemes.registry import SchemeRegistry

REFACTOR_STANDARD = SchemeDefinition(
    id="refactor_standard",
    name="Refactor Standard",
    description="Workflow for safe refactoring with comprehensive test verification",
    version="1.0.0",
    nodes=[
        SchemeNode(
            id="create_branch",
            name="Create Branch",
            node_type=NodeType.DETERMINISTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["git_ops"]),
            timeout_seconds=30,
        ),
        SchemeNode(
            id="refactor_code",
            name="Refactor Code",
            node_type=NodeType.AGENTIC,
            arsenal_requirement=ArsenalRequirement(
                tool_sets=["code_intel", "code_edit", "git_ops", "test_runner"]
            ),
            dossier_requirement=DossierRequirement(fetch_files=True, fetch_rules=True),
            max_steps=30,
            timeout_seconds=1200,
            model_name=ModelTier.COMPLEX.value,  # a tier, never a concrete model id
            instruction_template="Your refactoring instructions here...",
        ),
        # ... more nodes
    ],
    edges=[
        SchemeEdge(from_node="create_branch", to_node="refactor_code"),
        # ... more edges
    ],
)

SchemeRegistry.register(REFACTOR_STANDARD)
```

To reuse the lint/test/PR pipeline, build the nodes and edges with `standard_ci_pipeline(your_implement_node)` from `henchmen.schemes._shared_templates`, as `bugfix_standard` does. Every new deterministic node id needs a handler registered in `scheme_executor/handlers.py`.

### Step 2: Register for Auto-Discovery

The scheme module must be imported for registration to occur. Add it to the import list in:

- `src/henchmen/mastermind/server.py` (for the Mastermind service)
- `src/henchmen/operative/bootstrap.py` (for the Operative runtime)

```python
import henchmen.schemes.refactor_standard  # noqa: F401
```

Alternatively, call `SchemeRegistry.auto_discover()`, which imports every non-private module in the `schemes` package.

### Step 3: Add Scheme Selection Logic

Scheme selection lives in `MastermindAgent._select_scheme()` in `src/henchmen/mastermind/agent.py`. It checks, in order:

1. `_GOAL_KEYWORDS` against the **title** → `goal_decomposition`
2. The task's explicit `task_type` (`TaskType` in `src/henchmen/models/task.py`, sent as `task_type` on `POST /api/v1/tasks` and by `henchmen chat`): `bugfix` → `bugfix_standard`; `feature` or `refactor` → `feature_standard`
3. `_BUGFIX_KEYWORDS` against title and description → `bugfix_standard`
4. `_FEATURE_KEYWORDS` against title and description → `feature_standard`
5. Otherwise `bugfix_standard`

Matching is on word boundaries ("address" is not "add", "prefix" is not "fix"). An explicit type beats keywords, so a bugfix titled "Add null check" still runs `bugfix_standard`. Add a tuple for your scheme and check it at the right priority:

```python
_REFACTOR_KEYWORDS = ("refactor", "restructure", "reorganize", "simplify")

if _matches_keyword(title_lower, _REFACTOR_KEYWORDS):
    return "refactor_standard"
```

### Step 4: Validate

`SchemeRegistry.register()` validates the scheme and raises `ValueError` at import time, listing every problem:

- Node ids are unique
- Agentic nodes set both `instruction_template` and `model_name`; deterministic nodes set neither
- All edge references point to valid node IDs
- No fan-out: at most one outgoing edge per `(node, condition)` pair — the executor follows only the first match, so a second edge would be silently ignored
- Exactly one root node (no incoming edges)
- No cycles made only of unconditional edges
- All nodes are reachable from the root

The Mastermind additionally logs, at startup, any deterministic node that has no registered handler.

### Design Guidelines

1. **Start with deterministic nodes:** `create_branch` should always be the root. `create_pr` or `escalate` should be terminal.

2. **Use the cheapest tier that works:**
   - `default/light` for planning and classification
   - `default/complex` for core implementation
   - `default/reasoning` for diagnosis-heavy steps such as fixing tests or decomposing goals

3. **Add retry nodes for quality gates:** The pattern `run_check -> fix_check -> run_check_retry -> escalate` catches many issues automatically.

4. **Keep agentic steps focused:** A node with a 30-50 step budget and a focused instruction template works better than a 100-step node with a vague prompt.

5. **Prefer deterministic fixers:** `fix_lint` runs the linter's own `--fix` without an LLM, which is faster, cheaper, and more reliable than having an LLM fix whitespace issues.

6. **Write a specific instruction_template:** It is required on agentic nodes and is used verbatim as the system instruction; the task text is appended separately as untrusted input.

### Model Tiering Per Node

The `model_name` field on each `SchemeNode` names a tier. The configured LLM provider (`HENCHMEN_LLM_PROVIDER`, falling back to `HENCHMEN_PROVIDER`) resolves it to a concrete model through `henchmen.providers.tiers.resolve_model_name`. An agentic node must set a tier; anything that reaches the resolver without one is treated as `default/complex`.

| Tier | Best for | Anthropic | OpenAI | Vertex AI |
|------|----------|-----------|--------|-----------|
| `default/reasoning` | `fix_tests`, `analyze_goal` | `claude-opus-5` | `o3` | `gemini-3.1-pro` |
| `default/complex` | `implement_fix`, `implement_feature` | `claude-sonnet-5` | `gpt-4.1` | `gemini-2.5-pro` |
| `default/light` | planning, classification | `claude-haiku-4-5` | `gpt-4.1-mini` | `gemini-2.5-flash` |

Each cell is a `Settings` field, e.g. `HENCHMEN_ANTHROPIC_MODEL_COMPLEX` or `HENCHMEN_VERTEX_AI_MODEL_REASONING`. Bedrock reads `HENCHMEN_BEDROCK_MODEL_COMPLEX` / `_LIGHT` / `_REASONING`.

**Hard rule:** on Vertex AI, Henchmen uses Gemini exclusively. No Claude models on Vertex AI.

#### Recommended local (Ollama) models per tier

Ollama reads `HENCHMEN_LLM_OLLAMA_MODEL_COMPLEX` / `_LIGHT` / `_REASONING` and falls back to `HENCHMEN_LLM_OLLAMA_MODEL` (default `qwen2.5-coder:7b`) for any tier left empty, logging a warning that the tiering has been flattened. A model without native tool calling will not drive the operative loop.

| Tier | Setting | Recommended model |
|------|---------|-------------------|
| `default/complex` | `HENCHMEN_LLM_OLLAMA_MODEL_COMPLEX` | `qwen2.5-coder:7b` (or `qwen2.5-coder:14b` for more reliable tool calls) |
| `default/light` | `HENCHMEN_LLM_OLLAMA_MODEL_LIGHT` | `qwen2.5:3b` |
| `default/reasoning` | `HENCHMEN_LLM_OLLAMA_MODEL_REASONING` | `deepseek-r1:8b` |
