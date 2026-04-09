"""feature_standard scheme - standard workflow for implementing new features."""

from henchmen.models.scheme import (
    ArsenalRequirement,
    DossierRequirement,
    NodeType,
    SchemeDefinition,
    SchemeEdge,
    SchemeNode,
)
from henchmen.schemes.registry import SchemeRegistry

FEATURE_STANDARD = SchemeDefinition(
    id="feature_standard",
    name="Feature Standard",
    description=(
        "Standard workflow for implementing a new feature: branch, implement, "
        "then iterate through lint/test cycles until all checks pass before creating a PR. "
        "Only PRs with green checks are created — just like a real developer."
    ),
    version="3.0.0",
    nodes=[
        SchemeNode(
            id="create_branch",
            name="Create Branch",
            node_type=NodeType.DETERMINISTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["git_ops"]),
            timeout_seconds=30,
        ),
        SchemeNode(
            id="prefetch_context",
            name="Prefetch Context",
            node_type=NodeType.DETERMINISTIC,
            timeout_seconds=60,
        ),
        SchemeNode(
            id="implement_feature",
            name="Implement Feature",
            node_type=NodeType.AGENTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["code_intel", "code_edit", "git_ops", "test_runner"]),
            dossier_requirement=DossierRequirement(
                fetch_files=True,
                fetch_rules=True,
                fetch_related_issues=True,
                fetch_related_prs=True,
            ),
            max_steps=50,
            timeout_seconds=1800,
            model_name="gemini-2.5-pro",
            instruction_template=(
                "You are a coding operative implementing a feature.\n\n"
                "WORKFLOW:\n"
                "1. READ: Use grep_search and file_read to understand the codebase.\n"
                "   - Read existing files in the module for patterns, imports, types.\n"
                "2. IMPLEMENT: Write the code using file_edit and file_create.\n"
                "   - Follow the exact patterns you observed.\n"
                "   - If file_edit fails (old_text not found), file_read the file again and retry.\n"
                "3. VERIFY: Run type_check() to ensure your code compiles.\n"
                "   - If type_check fails, fix the errors and re-run.\n"
                "   - Optionally run_lint() and run_tests() to catch issues early.\n"
                "4. COMMIT: Call git_commit(message, files) to save your work.\n\n"
                "CRITICAL RULES:\n"
                "- Your task is NOT complete until you call git_commit. This is the ONLY thing that matters.\n"
                "- EVERY response MUST include a tool call. NEVER return text without calling a tool.\n"
                "- If you have nothing to read or edit, call git_commit immediately.\n"
                "- Do NOT return summaries, explanations, or analysis as text — use tools.\n"
                "- Run type_check() at least once before committing.\n"
                "- You have plenty of steps. Take the time to get it right, but ALWAYS call a tool."
            ),
        ),
        SchemeNode(
            id="verify_changes",
            name="Verify Changes",
            node_type=NodeType.DETERMINISTIC,
            timeout_seconds=30,
        ),
        # --- Lint cycle: run → agentic fix → retry ---
        SchemeNode(
            id="run_lint",
            name="Run Lint",
            node_type=NodeType.DETERMINISTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["test_runner"]),
            timeout_seconds=60,
        ),
        SchemeNode(
            id="fix_lint",
            name="Fix Lint",
            node_type=NodeType.AGENTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["code_edit", "code_intel", "test_runner", "git_ops"]),
            max_steps=15,
            timeout_seconds=600,
            model_name="gemini-2.5-pro",
            instruction_template=(
                "You are fixing lint errors. The previous lint check FAILED.\n"
                "The lint output is provided in the task description.\n\n"
                "1. Read the lint errors carefully\n"
                "2. For EACH error: file_read the file, file_edit to fix it\n"
                "3. After fixing all errors, run_lint() to verify\n"
                "4. If lint passes, git_commit your fixes\n"
                "5. If lint still fails, fix the remaining errors and try again\n\n"
                "Do NOT just run eslint --fix. You must understand and fix the actual errors.\n"
                "Common issues: missing imports, unused variables, type errors, wrong patterns."
            ),
        ),
        SchemeNode(
            id="run_lint_retry",
            name="Run Lint (Retry)",
            node_type=NodeType.DETERMINISTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["test_runner"]),
            timeout_seconds=60,
        ),
        # --- Test cycle: run → agentic fix → retry ---
        SchemeNode(
            id="run_tests",
            name="Run Tests",
            node_type=NodeType.DETERMINISTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["test_runner"]),
            timeout_seconds=300,
        ),
        SchemeNode(
            id="fix_tests",
            name="Fix Tests",
            node_type=NodeType.AGENTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["code_edit", "test_runner", "code_intel"]),
            max_steps=15,
            timeout_seconds=600,
            model_name="gemini-3.1-pro",
            grounding_enabled=True,
            instruction_template=(
                "You are fixing test failures. The previous test run FAILED.\n"
                "The test output is provided in the task description.\n\n"
                "1. Read the test errors carefully\n"
                "2. For EACH failure: file_read the failing test and the code it tests\n"
                "3. Fix the code (NOT the tests) unless the tests themselves are wrong\n"
                "4. After fixing, run_tests() to verify\n"
                "5. If tests pass, git_commit your fixes\n"
                "6. If tests still fail, fix the remaining failures and try again\n\n"
                "Do NOT skip failures. Do NOT delete tests. Fix the actual code."
            ),
        ),
        SchemeNode(
            id="run_tests_retry",
            name="Run Tests (Retry)",
            node_type=NodeType.DETERMINISTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["test_runner"]),
            timeout_seconds=300,
        ),
        # --- Terminal nodes ---
        SchemeNode(
            id="create_pr",
            name="Create PR",
            node_type=NodeType.DETERMINISTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["github"], allow_destructive=True),
            timeout_seconds=30,
        ),
        SchemeNode(
            id="escalate",
            name="Escalate",
            node_type=NodeType.DETERMINISTIC,
            arsenal_requirement=ArsenalRequirement(tool_sets=["slack"]),
            timeout_seconds=30,
        ),
    ],
    edges=[
        # Main path
        SchemeEdge(from_node="create_branch", to_node="prefetch_context"),
        SchemeEdge(from_node="prefetch_context", to_node="implement_feature"),
        SchemeEdge(from_node="implement_feature", to_node="verify_changes"),
        SchemeEdge(from_node="implement_feature", to_node="escalate", condition="fail"),
        SchemeEdge(from_node="verify_changes", to_node="run_lint", condition="pass"),
        SchemeEdge(from_node="verify_changes", to_node="escalate", condition="fail"),
        # Lint cycle: fail → agentic fix → retry. Only green lint proceeds.
        SchemeEdge(from_node="run_lint", to_node="run_tests", condition="pass"),
        SchemeEdge(from_node="run_lint", to_node="fix_lint", condition="fail"),
        SchemeEdge(from_node="fix_lint", to_node="run_lint_retry"),
        SchemeEdge(from_node="run_lint_retry", to_node="run_tests", condition="pass"),
        SchemeEdge(from_node="run_lint_retry", to_node="escalate", condition="fail"),
        # Test cycle: fail → agentic fix → retry. Only green tests proceed.
        SchemeEdge(from_node="run_tests", to_node="create_pr", condition="pass"),
        SchemeEdge(from_node="run_tests", to_node="fix_tests", condition="fail"),
        SchemeEdge(from_node="fix_tests", to_node="run_tests_retry"),
        SchemeEdge(from_node="run_tests_retry", to_node="create_pr", condition="pass"),
        SchemeEdge(from_node="run_tests_retry", to_node="escalate", condition="fail"),
    ],
)

SchemeRegistry.register(FEATURE_STANDARD)
