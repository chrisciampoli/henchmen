"""Goal Decomposition scheme - breaks high-level goals into concrete sub-tasks."""

from henchmen.models.llm import ModelTier
from henchmen.models.scheme import (
    ArsenalRequirement,
    DossierRequirement,
    NodeType,
    SchemeDefinition,
    SchemeEdge,
    SchemeNode,
)
from henchmen.schemes._shared_templates import PLAN_INSTRUCTION_TEMPLATE
from henchmen.schemes.registry import SchemeRegistry

GOAL_DECOMPOSITION = SchemeDefinition(
    id="goal_decomposition",
    name="Goal Decomposition",
    description="Analyzes a high-level goal and decomposes it into concrete, actionable sub-tasks",
    # 1.1.0 — analyze_goal uses the shared provider-neutral planning template
    # and references a model tier instead of a concrete Gemini id.
    version="1.1.0",
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
            model_name=ModelTier.REASONING.value,
            instruction_template=PLAN_INSTRUCTION_TEMPLATE,
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
