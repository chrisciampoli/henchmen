"""bugfix_standard scheme - standard workflow for fixing bugs."""

from henchmen.models.llm import ModelTier
from henchmen.models.scheme import (
    ArsenalRequirement,
    DossierRequirement,
    NodeType,
    SchemeDefinition,
    SchemeNode,
)
from henchmen.schemes._shared_templates import BUGFIX_INSTRUCTION_TEMPLATE, standard_ci_pipeline
from henchmen.schemes.registry import SchemeRegistry

_IMPLEMENT_FIX = SchemeNode(
    id="implement_fix",
    name="Implement Fix",
    node_type=NodeType.AGENTIC,
    arsenal_requirement=ArsenalRequirement(tool_sets=["code_intel", "code_edit", "git_ops", "test_runner", "context"]),
    dossier_requirement=DossierRequirement(
        fetch_files=True,
        fetch_rules=True,
        fetch_related_prs=True,
    ),
    max_steps=50,
    timeout_seconds=1800,
    model_name=ModelTier.COMPLEX.value,
    instruction_template=BUGFIX_INSTRUCTION_TEMPLATE,
)

_NODES, _EDGES = standard_ci_pipeline(_IMPLEMENT_FIX)

BUGFIX_STANDARD = SchemeDefinition(
    id="bugfix_standard",
    name="Bugfix Standard",
    description=(
        "Standard workflow for diagnosing and fixing a bug: branch, implement fix, "
        "then iterate through lint/test cycles until all checks pass before creating a PR. "
        "Only PRs with green checks are created — just like a real developer."
    ),
    # 3.0.0 — nodes reference model tiers instead of concrete Gemini ids and
    # fix_lint is deterministic (no LLM, no Lair).
    version="3.0.0",
    nodes=_NODES,
    edges=_EDGES,
)

SchemeRegistry.register(BUGFIX_STANDARD)
