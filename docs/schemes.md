# Scheme Reference

## What is a Scheme?

A Scheme is a directed acyclic graph (DAG) that defines the workflow an operative follows to complete a task. Each node in the graph is either a deterministic step (runs inline code without an LLM) or an agentic step (provisions an ephemeral container with an LLM agent). Edges connect nodes with optional conditions (`pass`/`fail`) to create branching workflows with retry loops.

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

Each node has:

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` | Unique identifier within the scheme (e.g., `implement_fix`) |
| `name` | `str` | Human-readable name |
| `node_type` | `NodeType` | `DETERMINISTIC` or `AGENTIC` |
| `arsenal_requirement` | `ArsenalRequirement` | Which tool sets the operative can access |
| `dossier_requirement` | `DossierRequirement` | What context to pre-fetch |
| `max_steps` | `int` | Max agentic loop iterations (default: 20) |
| `timeout_seconds` | `int` | Execution timeout (default: 300) |
| `instruction_template` | `str` | System prompt for the agentic loop |
| `model_name` | `str` | Override model for this node |

### SchemeEdge

```python
class SchemeEdge(BaseModel):
    from_node: str                           # Source node ID
    to_node: str                             # Destination node ID
    condition: Literal["pass", "fail"] | None  # Edge condition (None = unconditional)
```

### Node Types

**DETERMINISTIC** nodes run inline handlers in the `SchemeExecutor`. They do not use an LLM. They execute fast, predictable operations:

| Handler ID | What It Does |
|------------|-------------|
| `create_branch` | Generates branch name `henchmen/{task_id[:8]}` |
| `prefetch_context` | Returns dossier artifact URI |
| `run_lint` / `run_lint_retry` | Clones the branch, runs lint (eslint for Node.js, ruff for Python). Only lints files changed by the operative (not pre-existing warnings). |
| `fix_lint` | Runs `eslint --fix` or `ruff --fix`, commits and pushes auto-fixed files. No LLM needed. |
| `run_tests` / `run_tests_retry` | Clones the branch, runs tests (jest for Node.js, pytest for Python) |
| `create_pr` | Opens a GitHub pull request via the GitHub API |
| `escalate` | Marks the task for human review |
| `report_plan` | Reports goal decomposition results |

**AGENTIC** nodes are dispatched to Lairs (Cloud Run Jobs). The `LairManager` creates a Cloud Run Job with the operative container image, injects environment variables (task ID, node ID, model name, repo URL, etc.), and polls the execution until completion.

### Edge Conditions

Edges can be:

- **Unconditional** (`condition=None`): Always followed. Used for linear flow (e.g., `create_branch -> prefetch_context`).
- **Conditional** (`condition="pass"` or `condition="fail"`): Followed based on the node's result. Deterministic nodes return `{"condition": "pass"}` or `{"condition": "fail"}` based on lint/test exit codes. Agentic nodes return `pass` if the operative completed successfully, `fail` otherwise.

When a node returns a condition but no matching conditional edge exists, the executor falls back to unconditional edges. If no edges match at all, the node is treated as terminal.

### Fail-Closed Gates

The system is designed to fail closed:

1. **Lint fails -> auto-fix -> re-lint -> escalate:** If lint fails after auto-fix, the task escalates to a human. It does not proceed to PR creation.
2. **Tests fail -> LLM fix -> re-test -> escalate:** If tests fail after the LLM fix attempt, the task escalates.
3. **Lair provisioning fails in production:** The node returns `fail` and the task follows the failure path (typically escalation). In dev mode only, lair failures are simulated as passes for pipeline testing.
4. **CI check errors:** If lint or test commands cannot even run (clone failure, missing dependencies), the node returns `fail` rather than silently passing.
5. **Max node retries:** Each node can be executed at most 2 times (tracked by `SchemeExecutor._retry_counts`). This prevents infinite loops in verify -> implement retry cycles.

### Controlled Retry Loops

Some edges create cycles in the graph (e.g., `verify_changes --fail--> implement_fix`). These are intentional retry loops. The scheme validator distinguishes between:

- **Unconditional cycles:** Detected and rejected during validation (would cause infinite loops)
- **Conditional cycles:** Allowed because they are controlled by `pass`/`fail` conditions and bounded by the `_max_node_retries` limit (2)

## Current Schemes

### bugfix_standard

**File:** `src/henchmen/schemes/bugfix_standard.py`
**Triggered by:** Keywords like "bug", "fix", "error", "crash", "broken"

```
create_branch
    |
prefetch_context
    |
implement_fix  <--------+
    |                    |
verify_changes           |
    |  pass    |  fail --+
    v          |
run_lint       |
    |  pass    |  fail
    v          v
run_tests    fix_lint
    |  pass    |
    v          v
create_pr    run_lint_retry
             |  pass    |  fail
             v          v
           run_tests   escalate
             |  pass    |  fail
             v          v
           fix_tests   escalate
             |
             v
           run_tests_retry
             |  pass    |  fail
             v          v
           create_pr   escalate
```

**Node details:**

| Node | Type | Model | Steps | Timeout |
|------|------|-------|-------|---------|
| `create_branch` | DETERMINISTIC | -- | -- | 30s |
| `prefetch_context` | DETERMINISTIC | -- | -- | 60s |
| `implement_fix` | AGENTIC | Claude Sonnet 4 | 40 | 1800s |
| `verify_changes` | AGENTIC | Gemini 2.5 Flash | 10 | 600s |
| `run_lint` | DETERMINISTIC | -- | -- | 60s |
| `fix_lint` | DETERMINISTIC | -- | -- | 120s |
| `run_lint_retry` | DETERMINISTIC | -- | -- | 60s |
| `run_tests` | DETERMINISTIC | -- | -- | 300s |
| `fix_tests` | AGENTIC | Gemini 3.1 Pro | 15 | 300s |
| `run_tests_retry` | DETERMINISTIC | -- | -- | 300s |
| `create_pr` | DETERMINISTIC | -- | -- | 30s |
| `escalate` | DETERMINISTIC | -- | -- | 30s |

**implement_fix instruction template:** The operative follows a strict 4-phase workflow:
1. INVESTIGATE (steps 1-3): Read 1-3 key files to understand the bug
2. FIX (steps 4-6): Make the minimal code change
3. VERIFY (before commit): Run type_check, run_lint, run_tests -- all must pass
4. COMMIT: Call git_commit with a descriptive message

**verify_changes instruction template:** Uses `git_diff()` to check that:
- Files were actually modified (not just temp files)
- Changes address the original task
- Changes are clean (no duplicates, no junk)
- Security controls were not removed (rate limiting, auth guards, input validation, CORS)

### feature_standard

**File:** `src/henchmen/schemes/feature_standard.py`
**Triggered by:** Keywords like "feature", "implement", "build", "create", "add", "new module", "setup", "scaffold", "portal", "dashboard"

The feature scheme adds a planning step before implementation:

```
create_branch
    |
prefetch_context
    |
plan_implementation   (AGENTIC, Gemini 2.5 Flash, 10 steps)
    |
implement_feature     (AGENTIC, Claude Sonnet 4, 40 steps)
    |
verify_changes  <-----(same retry/lint/test structure as bugfix)
    |
run_lint -> fix_lint -> run_lint_retry
    |
run_tests -> fix_tests -> run_tests_retry
    |
create_pr / escalate
```

**Key difference from bugfix_standard:** The `plan_implementation` node uses Gemini 2.5 Flash (fast and cheap) to explore the codebase with read-only tools (`code_intel`) and create an implementation plan before the expensive Claude Sonnet 4 `implement_feature` step begins.

### goal_decomposition

**File:** `src/henchmen/schemes/goal_decomposition.py`
**Triggered by:** Keywords like "improve", "optimize", "refactor all", "fix all", "update all", "increase coverage", "reduce", "clean up all", "migrate"

This is a lightweight planning-only scheme:

```
analyze_goal -> report_plan
```

| Node | Type | Model | Steps | Timeout |
|------|------|-------|-------|---------|
| `analyze_goal` | AGENTIC | Gemini 3.1 Pro Preview (customtools) | 5 | 300s |
| `report_plan` | DETERMINISTIC | -- | -- | 30s |

**analyze_goal instruction template:** The operative explores the codebase using read-only tools and produces 3-5 specific, concrete sub-tasks in a structured format:
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
            max_steps=30,
            timeout_seconds=1200,
            model_name="claude-sonnet-4@20250514",
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

### Step 2: Register for Auto-Discovery

The scheme module must be imported for registration to occur. Add it to the import list in:

- `src/henchmen/mastermind/server.py` (for the Mastermind service)
- `src/henchmen/operative/bootstrap.py` (for the Operative runtime)

```python
import henchmen.schemes.refactor_standard  # noqa: F401
```

Alternatively, call `SchemeRegistry.auto_discover()` which imports all modules in the `schemes` package automatically.

### Step 3: Add Scheme Selection Logic

Update `MastermindAgent._select_scheme()` in `src/henchmen/mastermind/agent.py` to route tasks to your new scheme:

```python
refactor_keywords = ["refactor", "restructure", "reorganize", "simplify"]
if any(kw in title_lower for kw in refactor_keywords):
    return "refactor_standard"
```

### Step 4: Validate

The `SchemeRegistry.register()` method validates the DAG on registration:

- All edge references must point to valid node IDs
- Exactly one root node (no incoming edges)
- No unconditional cycles
- All nodes must be reachable from the root

If validation fails, a `ValueError` is raised at import time with detailed error messages.

### Design Guidelines

1. **Start with deterministic nodes:** `create_branch` should always be the root. `create_pr` or `escalate` should be terminal.

2. **Use the cheapest model that works:**
   - Verification and planning: Gemini 2.5 Flash (fast, cheap)
   - Test fixes: Gemini 3.1 Pro (good reasoning, moderate cost)
   - Core implementation: Claude Sonnet 4 (best coding quality, highest cost)

3. **Add retry loops for quality gates:** The pattern `run_check -> fix_check -> run_check_retry -> escalate` catches many issues automatically.

4. **Keep agentic steps focused:** A node with `max_steps=40` and a focused instruction template works better than `max_steps=100` with a vague prompt.

5. **Use deterministic fix_lint:** The `fix_lint` node runs `eslint --fix` or `ruff --fix` without an LLM, which is faster, cheaper, and more reliable than having an LLM fix whitespace issues.

6. **Set instruction_template on agentic nodes:** Without it, the operative falls back to generic prompt templates which lack workflow-specific guidance. The scheme author's instruction_template is always highest priority.

### Model Tiering Per Node

The `model_name` field on each `SchemeNode` determines the LLM used. If not set, it falls back to the `vertex_ai_model_complex` setting (Claude Sonnet 4 by default).

Available models and their recommended use:

| Model | Price Tier | Best For |
|-------|-----------|----------|
| `claude-sonnet-4@20250514` | High | Core code generation, complex bug fixes |
| `gemini-3.1-pro` | Medium | Test fixing, moderate reasoning tasks |
| `gemini-3.1-pro-preview-customtools` | Medium | Goal analysis with custom tools |
| `gemini-2.5-flash` | Low | Verification gates, planning, classification |
| `gemini-2.5-pro` | Medium | General-purpose fallback |

The operative agent handles model routing automatically: model names containing "claude" route to `_call_claude` (via Anthropic-on-Vertex), all others to `_call_gemini` (via google-genai SDK). Claude calls automatically fall back to Gemini on rate limits or unavailability.
