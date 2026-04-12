"""Scheme models - defines workflow graphs that operatives execute."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from henchmen.models._base import StrictBase


class NodeType(StrEnum):
    DETERMINISTIC = "deterministic"
    AGENTIC = "agentic"


class ArsenalRequirement(StrictBase):
    """Specifies which tool sets an operative node requires from the Arsenal MCP server."""

    tool_sets: list[
        Literal["code_intel", "code_edit", "git_ops", "test_runner", "github", "jira", "slack", "gcp", "context"]
    ] = Field(default_factory=list, description="List of required Arsenal tool sets")
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

    base_steps: int = Field(default=20, description="Initial step budget")
    min_steps: int = Field(default=10, description="Minimum steps before early exit is allowed")
    max_steps: int = Field(default=30, description="Absolute maximum including extensions")
    extension_steps: int = Field(default=10, description="Steps granted per extension")
    max_extensions: int = Field(default=2, description="Maximum number of extensions")
    early_exit_on_commit: bool = Field(default=True, description="Allow early exit when git_commit succeeds")


# Default budgets per node type
STEP_BUDGET_DEFAULTS: dict[str, StepBudget] = {
    "fix_lint": StepBudget(base_steps=10, min_steps=5, max_steps=15, extension_steps=5, max_extensions=1),
    "verify_changes": StepBudget(base_steps=15, min_steps=10, max_steps=20, extension_steps=5, max_extensions=1),
    "implement_fix": StepBudget(base_steps=30, min_steps=15, max_steps=50, extension_steps=10, max_extensions=2),
    "implement_feature": StepBudget(base_steps=50, min_steps=20, max_steps=70, extension_steps=10, max_extensions=2),
    "fix_tests": StepBudget(base_steps=40, min_steps=15, max_steps=60, extension_steps=10, max_extensions=2),
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
    acceptance_check: str | None = Field(
        default=None,
        description="Dotted Python path to an acceptance check function (e.g. 'henchmen.schemes.checks.tests_pass')",
    )
    max_steps: int = Field(default=20, description="Maximum agentic steps before forced termination")
    step_budget: StepBudget | None = Field(
        default=None, description="Adaptive step budget (overrides max_steps when set)"
    )
    timeout_seconds: int = Field(default=300, description="Node execution timeout in seconds")
    instruction_template: str | None = Field(
        default=None, description="Jinja2 template string for the operative's system instruction"
    )
    model_name: str | None = Field(
        default=None, description="Override Vertex AI model for this node (falls back to OperativeConfig)"
    )
    grounding_enabled: bool = Field(
        default=False, description="Enable Google Search grounding for real error resolution"
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
