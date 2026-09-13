"""Scheme models - defines workflow graphs that operatives execute."""

from enum import StrEnum
from typing import Literal, get_args

from pydantic import Field, model_validator

from henchmen.models._base import StrictBase


class NodeType(StrEnum):
    DETERMINISTIC = "deterministic"
    AGENTIC = "agentic"


# Arsenal tool categories. Every value here must be registered by a module in
# ``henchmen.arsenal.tools`` — ``ToolRegistry.get_tools_for_requirement`` returns
# an empty list (silently, no tools) for a category nothing registers, so an
# invented category would hand the operative a toolless node.
ArsenalToolSet = Literal[
    "code_edit",
    "code_intel",
    "context",
    "git_ops",
    "github",
    "jira",
    "slack",
    "test_runner",
]

ARSENAL_TOOL_SETS: frozenset[str] = frozenset(get_args(ArsenalToolSet))


class ArsenalRequirement(StrictBase):
    """Specifies which tool sets an operative node requires from the Arsenal MCP server."""

    tool_sets: list[ArsenalToolSet] = Field(
        default_factory=list,
        description="Required Arsenal tool categories (must match a category registered by henchmen.arsenal.tools)",
    )
    allow_destructive: bool = Field(default=False, description="Whether destructive operations are permitted")


class DossierRequirement(StrictBase):
    """Specifies what context the Dossier builder should fetch for a node."""

    fetch_files: bool = Field(default=False, description="Fetch relevant source files")
    fetch_rules: bool = Field(default=False, description="Fetch repo rule files (CLAUDE.md, etc.)")
    fetch_related_prs: bool = Field(default=False, description="Fetch related pull requests")
    fetch_related_issues: bool = Field(default=False, description="Fetch related issues/tickets")
    code_search_symbols: list[str] = Field(
        default_factory=list, description="Symbol names to pre-fetch via code search"
    )


class StepBudget(StrictBase):
    """Adaptive step budget configuration for agentic nodes.

    Instead of a single ``max_steps`` hard cap, this model supports
    extensions on progress (e.g. a successful commit grants extra steps)
    and early exit when the agent commits before hitting the limit.
    """

    base_steps: int = Field(default=20, ge=1, description="Initial step budget")
    min_steps: int = Field(default=10, ge=1, description="Minimum steps before early exit is allowed")
    max_steps: int = Field(default=30, ge=1, description="Absolute maximum including extensions")
    extension_steps: int = Field(default=10, ge=0, description="Steps granted per extension")
    max_extensions: int = Field(default=2, ge=0, description="Maximum number of extensions")
    early_exit_on_commit: bool = Field(default=True, description="Allow early exit when git_commit succeeds")

    @model_validator(mode="after")
    def _check_bounds(self) -> "StepBudget":
        if self.base_steps > self.max_steps:
            raise ValueError(f"base_steps ({self.base_steps}) must not exceed max_steps ({self.max_steps})")
        return self


# Default budgets for the agentic nodes shipped with Henchmen. Keys MUST be
# agentic node ids — a deterministic node never runs an agent loop, so a budget
# keyed on one is dead weight that drifts silently (see the test in
# tests/unit/test_schemes.py that pins this against the registered schemes).
STEP_BUDGET_DEFAULTS: dict[str, StepBudget] = {
    "implement_fix": StepBudget(base_steps=30, min_steps=15, max_steps=50, extension_steps=10, max_extensions=2),
    "implement_feature": StepBudget(base_steps=50, min_steps=20, max_steps=70, extension_steps=10, max_extensions=2),
    "fix_tests": StepBudget(base_steps=15, min_steps=5, max_steps=25, extension_steps=5, max_extensions=2),
    "analyze_goal": StepBudget(base_steps=5, min_steps=3, max_steps=10, extension_steps=5, max_extensions=1),
}


class SchemeNode(StrictBase):
    """A single node in a scheme workflow graph."""

    id: str = Field(..., description="Unique node identifier within the scheme")
    name: str = Field(..., description="Human-readable node name")
    node_type: NodeType = Field(..., description="Whether the node is deterministic or agentic")
    arsenal_requirement: ArsenalRequirement | None = Field(
        default=None, description="Arsenal tool requirements for this node"
    )
    dossier_requirement: DossierRequirement | None = Field(
        default=None, description="Dossier context requirements for this node"
    )
    max_steps: int = Field(default=20, ge=1, description="Maximum agentic steps before forced termination")
    step_budget: StepBudget | None = Field(
        default=None, description="Adaptive step budget (overrides max_steps when set)"
    )
    timeout_seconds: int = Field(default=300, ge=1, description="Node execution timeout in seconds")
    instruction_template: str | None = Field(
        default=None,
        description=(
            "Static system-instruction text for the operative. Used verbatim — there is no "
            "templating engine; task text is appended separately as untrusted input."
        ),
    )
    model_name: str | None = Field(
        default=None,
        description=(
            "Model tier (a ModelTier value such as 'default/complex') or a concrete model id. "
            "Resolved per provider by henchmen.providers.tiers.resolve_model_name; "
            "falls back to the COMPLEX tier when unset."
        ),
    )
    grounding_enabled: bool = Field(
        default=False,
        description=(
            "Request Google Search grounding for this node. Only the Vertex AI (Gemini) "
            "direct-SDK path honours it; every other provider ignores it."
        ),
    )

    def get_effective_budget(self) -> StepBudget:
        """Return the step budget, falling back to defaults or constructing from max_steps."""
        if self.step_budget is not None:
            return self.step_budget
        default = STEP_BUDGET_DEFAULTS.get(self.id)
        if default is not None:
            return default
        return StepBudget(
            base_steps=self.max_steps,
            min_steps=max(5, self.max_steps // 3),
            max_steps=self.max_steps,
            extension_steps=0,
            max_extensions=0,
        )


class SchemeEdge(StrictBase):
    """A directed edge connecting two nodes in a scheme workflow graph."""

    from_node: str = Field(..., description="Source node ID")
    to_node: str = Field(..., description="Destination node ID")
    condition: Literal["pass", "fail"] | None = Field(
        default=None,
        description="Edge condition: 'pass' or 'fail' based on node outcome, or None for unconditional",
    )


class SchemeDefinition(StrictBase):
    """A complete workflow scheme definition."""

    id: str = Field(..., description="Unique scheme identifier")
    name: str = Field(..., description="Human-readable scheme name")
    description: str = Field(..., description="What this scheme accomplishes")
    version: str = Field(..., description="Semantic version of the scheme definition")
    nodes: list[SchemeNode] = Field(..., description="Ordered list of workflow nodes")
    edges: list[SchemeEdge] = Field(default_factory=list, description="Directed edges defining execution flow")
