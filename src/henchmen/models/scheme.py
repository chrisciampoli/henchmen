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

    tool_sets: list[Literal["code_intel", "code_edit", "git_ops", "test_runner", "github", "jira", "slack", "gcp"]] = (
        Field(default_factory=list, description="List of required Arsenal tool sets")
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
