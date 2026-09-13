"""feature_standard scheme - standard workflow for implementing new features."""

from henchmen.models.llm import ModelTier
from henchmen.models.scheme import (
    ArsenalRequirement,
    DossierRequirement,
    NodeType,
    SchemeDefinition,
    SchemeNode,
)
from henchmen.schemes._shared_templates import FEATURE_INSTRUCTION_TEMPLATE, standard_ci_pipeline
from henchmen.schemes.registry import SchemeRegistry

_IMPLEMENT_FEATURE = SchemeNode(
    id="implement_feature",
    name="Implement Feature",
    node_type=NodeType.AGENTIC,
    arsenal_requirement=ArsenalRequirement(tool_sets=["code_intel", "code_edit", "git_ops", "test_runner", "context"]),
    dossier_requirement=DossierRequirement(
        fetch_files=True,
        fetch_rules=True,
        fetch_related_issues=True,
        fetch_related_prs=True,
    ),
    max_steps=50,
    timeout_seconds=1800,
    model_name=ModelTier.COMPLEX.value,
    instruction_template=FEATURE_INSTRUCTION_TEMPLATE,
)

_NODES, _EDGES = standard_ci_pipeline(_IMPLEMENT_FEATURE)

FEATURE_STANDARD = SchemeDefinition(
    id="feature_standard",
    name="Feature Standard",
    description=(
        "Standard workflow for implementing a new feature: branch, implement, "
        "then iterate through lint/test cycles until all checks pass before creating a PR. "
        "Only PRs with green checks are created — just like a real developer."
    ),
    # 4.0.0 — nodes reference model tiers instead of concrete Gemini ids and
    # fix_lint is deterministic (no LLM, no Lair).
    version="4.0.0",
    nodes=_NODES,
    edges=_EDGES,
)

SchemeRegistry.register(FEATURE_STANDARD)
