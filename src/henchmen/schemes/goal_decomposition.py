"""Goal Decomposition scheme - breaks high-level goals into concrete sub-tasks."""

from henchmen.models.scheme import (
    ArsenalRequirement,
    DossierRequirement,
    NodeType,
    SchemeDefinition,
    SchemeEdge,
    SchemeNode,
)
from henchmen.schemes.registry import SchemeRegistry

GOAL_DECOMPOSITION = SchemeDefinition(
    id="goal_decomposition",
    name="Goal Decomposition",
    description="Analyzes a high-level goal and decomposes it into concrete, actionable sub-tasks",
    version="1.0.0",
    nodes=[
        SchemeNode(
            id="analyze_goal",
            name="Analyze Goal",
            node_type=NodeType.AGENTIC,
            arsenal_requirement=ArsenalRequirement(
                tool_sets=["code_intel"],  # Read-only, for exploring the codebase
            ),
            dossier_requirement=DossierRequirement(
                fetch_files=True,
                fetch_rules=True,
            ),
            max_steps=5,
            timeout_seconds=300,
            model_name="gemini-3.1-pro-preview-customtools",
            instruction_template=(
                "You are a task planner analyzing a codebase to decompose a high-level goal into concrete tasks.\n\n"
                "Available tools:\n"
                "- file_search(pattern, directory): Find files by name\n"
                "- grep_search(pattern, directory): Search file contents\n"
                "- file_read(path): Read file contents\n\n"
                "INSTRUCTIONS:\n"
                "1. Read the goal description in the task\n"
                "2. Explore the codebase to understand the current state\n"
                "3. Identify 3-5 SPECIFIC, CONCRETE sub-tasks that would accomplish the goal\n"
                "4. For each sub-task, specify: the exact file(s) to change, what change to make, and why\n\n"
                "OUTPUT FORMAT (you MUST follow this exactly):\n"
                "SUBTASK 1: [title]\n"
                "FILES: [file1.py, file2.py]\n"
                "CHANGE: [specific description of what to change]\n\n"
                "SUBTASK 2: [title]\n"
                "FILES: [file3.py]\n"
                "CHANGE: [specific description]\n\n"
                "... etc.\n\n"
                "Be SPECIFIC. 'Fix the auth module' is too vague. "
                "'Add input validation to the login endpoint in auth.service.ts "
                "to check for empty passwords' is specific."
            ),
        ),
        SchemeNode(
            id="report_plan",
            name="Report Plan",
            node_type=NodeType.DETERMINISTIC,
            timeout_seconds=30,
        ),
    ],
    edges=[
        SchemeEdge(from_node="analyze_goal", to_node="report_plan"),
    ],
)

SchemeRegistry.register(GOAL_DECOMPOSITION)
