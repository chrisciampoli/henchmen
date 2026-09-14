"""Unit tests for SchemeGraph, SchemeRegistry and the registered scheme definitions."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from henchmen.models.llm import ModelTier
from henchmen.models.scheme import (
    ARSENAL_TOOL_SETS,
    STEP_BUDGET_DEFAULTS,
    ArsenalRequirement,
    NodeType,
    SchemeDefinition,
    SchemeEdge,
    SchemeNode,
    StepBudget,
)
from henchmen.schemes.base import SchemeGraph
from henchmen.schemes.registry import SchemeRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(node_id: str, node_type: NodeType = NodeType.AGENTIC, **overrides: Any) -> SchemeNode:
    """Build a *valid* node for graph-shape tests.

    Defaults to an agentic node because deterministic nodes must not carry a
    prompt or a model, and agentic nodes must carry both — the helper keeps
    every synthetic scheme in this module semantically valid so the topology
    assertions are not drowned in node-invariant errors.
    """
    kwargs: dict[str, Any] = {
        "id": node_id,
        "name": node_id.replace("_", " ").title(),
        "node_type": node_type,
    }
    if node_type == NodeType.AGENTIC:
        kwargs["model_name"] = ModelTier.LIGHT.value
        kwargs["instruction_template"] = "Test instruction."
    kwargs.update(overrides)
    return SchemeNode(**kwargs)


def _make_edge(from_node: str, to_node: str, condition=None) -> SchemeEdge:
    return SchemeEdge(from_node=from_node, to_node=to_node, condition=condition)


def _linear_scheme(node_ids: list[str]) -> SchemeDefinition:
    """Build a simple linear scheme: a → b → c → ..."""
    nodes = [_make_node(nid) for nid in node_ids]
    edges = [_make_edge(node_ids[i], node_ids[i + 1]) for i in range(len(node_ids) - 1)]
    return SchemeDefinition(
        id="linear_test",
        name="Linear Test",
        description="Linear test scheme",
        version="0.0.1",
        nodes=nodes,
        edges=edges,
    )


# ---------------------------------------------------------------------------
# SchemeGraph.validate() — invalid cases
# ---------------------------------------------------------------------------


class TestSchemeGraphValidateInvalid:
    def test_invalid_edge_from_node(self):
        scheme = SchemeDefinition(
            id="bad_from",
            name="Bad From",
            description="d",
            version="0.0.1",
            nodes=[_make_node("a"), _make_node("b")],
            edges=[_make_edge("a", "b"), _make_edge("nonexistent", "b")],
        )
        errors = SchemeGraph(scheme).validate()
        assert any("nonexistent" in e for e in errors)

    def test_invalid_edge_to_node(self):
        scheme = SchemeDefinition(
            id="bad_to",
            name="Bad To",
            description="d",
            version="0.0.1",
            nodes=[_make_node("a"), _make_node("b")],
            edges=[_make_edge("a", "b"), _make_edge("a", "ghost")],
        )
        errors = SchemeGraph(scheme).validate()
        assert any("ghost" in e for e in errors)

    def test_cycle_detected(self):
        # a → b → c → b  (cycle between b and c)
        scheme = SchemeDefinition(
            id="cyclic",
            name="Cyclic",
            description="d",
            version="0.0.1",
            nodes=[_make_node("a"), _make_node("b"), _make_node("c")],
            edges=[
                _make_edge("a", "b"),
                _make_edge("b", "c"),
                _make_edge("c", "b"),
            ],
        )
        errors = SchemeGraph(scheme).validate()
        assert any("cycle" in e.lower() for e in errors)

    def test_multiple_root_nodes(self):
        # Both a and b have no incoming edges
        scheme = SchemeDefinition(
            id="multi_root",
            name="Multi Root",
            description="d",
            version="0.0.1",
            nodes=[_make_node("a"), _make_node("b"), _make_node("c")],
            edges=[
                _make_edge("a", "c"),
                _make_edge("b", "c"),
            ],
        )
        errors = SchemeGraph(scheme).validate()
        assert any("multiple root" in e.lower() for e in errors)

    def test_unreachable_node(self):
        # d exists but is not connected to the main graph
        scheme = SchemeDefinition(
            id="unreachable",
            name="Unreachable",
            description="d",
            version="0.0.1",
            nodes=[_make_node("a"), _make_node("b"), _make_node("c"), _make_node("d")],
            edges=[
                _make_edge("a", "b"),
                _make_edge("b", "c"),
                # d is an island
            ],
        )
        errors = SchemeGraph(scheme).validate()
        assert any("unreachable" in e.lower() or "'d'" in e for e in errors)


# ---------------------------------------------------------------------------
# SchemeGraph.validate() — valid cases
# ---------------------------------------------------------------------------


class TestSchemeGraphValidateValid:
    def test_linear_scheme_is_valid(self):
        graph = SchemeGraph(_linear_scheme(["a", "b", "c"]))
        assert graph.validate() == []

    def test_diamond_scheme_is_valid(self):
        # root → left, root → right; left → sink, right → sink
        scheme = SchemeDefinition(
            id="diamond",
            name="Diamond",
            description="d",
            version="0.0.1",
            nodes=[_make_node("root"), _make_node("left"), _make_node("right"), _make_node("sink")],
            edges=[
                _make_edge("root", "left", condition="pass"),
                _make_edge("root", "right", condition="fail"),
                _make_edge("left", "sink"),
                _make_edge("right", "sink"),
            ],
        )
        graph = SchemeGraph(scheme)
        assert graph.validate() == []

    def test_bugfix_standard_is_valid(self):
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD

        graph = SchemeGraph(BUGFIX_STANDARD)
        assert graph.validate() == []

    def test_feature_standard_is_valid(self):
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        graph = SchemeGraph(FEATURE_STANDARD)
        assert graph.validate() == []


# ---------------------------------------------------------------------------
# SchemeGraph.topological_sort()
# ---------------------------------------------------------------------------


class TestSchemeGraphTopologicalSort:
    def test_linear_order(self):
        graph = SchemeGraph(_linear_scheme(["a", "b", "c"]))
        order = [n.id for n in graph.topological_sort()]
        assert order.index("a") < order.index("b") < order.index("c")

    def test_all_nodes_present(self):
        graph = SchemeGraph(_linear_scheme(["x", "y", "z"]))
        order = graph.topological_sort()
        assert {n.id for n in order} == {"x", "y", "z"}

    def test_raises_on_cycle(self):
        scheme = SchemeDefinition(
            id="cyclic_sort",
            name="Cyclic Sort",
            description="d",
            version="0.0.1",
            nodes=[_make_node("a"), _make_node("b")],
            edges=[_make_edge("a", "b"), _make_edge("b", "a")],
        )
        with pytest.raises(ValueError, match="[Cc]ycle"):
            SchemeGraph(scheme).topological_sort()

    def test_bugfix_standard_topo_succeeds(self):
        # bugfix_standard no longer has controlled loops (verify_changes fail -> escalate),
        # so topological sort should succeed.
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD

        graph = SchemeGraph(BUGFIX_STANDARD)
        order = graph.topological_sort()
        assert len(order) > 0


# ---------------------------------------------------------------------------
# SchemeGraph.get_root_node()
# ---------------------------------------------------------------------------


class TestSchemeGraphGetRootNode:
    def test_returns_entry_node(self):
        graph = SchemeGraph(_linear_scheme(["entry", "middle", "end"]))
        assert graph.get_root_node().id == "entry"

    def test_bugfix_root_is_create_branch(self):
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD

        graph = SchemeGraph(BUGFIX_STANDARD)
        assert graph.get_root_node().id == "create_branch"

    def test_feature_root_is_create_branch(self):
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        graph = SchemeGraph(FEATURE_STANDARD)
        assert graph.get_root_node().id == "create_branch"

    def test_raises_for_multi_root(self):
        scheme = SchemeDefinition(
            id="mr",
            name="Multi Root",
            description="d",
            version="0.0.1",
            nodes=[_make_node("a"), _make_node("b"), _make_node("c")],
            edges=[_make_edge("a", "c"), _make_edge("b", "c")],
        )
        with pytest.raises(ValueError):
            SchemeGraph(scheme).get_root_node()


# ---------------------------------------------------------------------------
# SchemeGraph.get_next_nodes()
# ---------------------------------------------------------------------------


class TestSchemeGraphGetNextNodes:
    def test_unconditional_successor(self):
        graph = SchemeGraph(_linear_scheme(["a", "b", "c"]))
        nexts = graph.get_next_nodes("a")
        assert [n.id for n in nexts] == ["b"]

    def test_conditional_pass(self):
        scheme = SchemeDefinition(
            id="cond",
            name="Cond",
            description="d",
            version="0.0.1",
            nodes=[_make_node("root"), _make_node("pass_node"), _make_node("fail_node")],
            edges=[
                _make_edge("root", "pass_node", condition="pass"),
                _make_edge("root", "fail_node", condition="fail"),
            ],
        )
        graph = SchemeGraph(scheme)
        assert [n.id for n in graph.get_next_nodes("root", condition="pass")] == ["pass_node"]
        assert [n.id for n in graph.get_next_nodes("root", condition="fail")] == ["fail_node"]

    def test_no_match_returns_empty(self):
        graph = SchemeGraph(_linear_scheme(["a", "b"]))
        # The edge is unconditional (condition=None); querying "pass" yields nothing
        assert graph.get_next_nodes("a", condition="pass") == []

    def test_terminal_node_has_no_successors(self):
        graph = SchemeGraph(_linear_scheme(["a", "b", "c"]))
        assert graph.get_next_nodes("c") == []

    def test_bugfix_run_lint_pass_goes_to_run_tests(self):
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD

        graph = SchemeGraph(BUGFIX_STANDARD)
        nexts = graph.get_next_nodes("run_lint", condition="pass")
        assert [n.id for n in nexts] == ["run_tests"]

    def test_bugfix_run_lint_fail_goes_to_fix_lint(self):
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD

        graph = SchemeGraph(BUGFIX_STANDARD)
        nexts = graph.get_next_nodes("run_lint", condition="fail")
        assert [n.id for n in nexts] == ["fix_lint"]


# ---------------------------------------------------------------------------
# SchemeGraph.get_terminal_nodes()
# ---------------------------------------------------------------------------


class TestSchemeGraphGetTerminalNodes:
    def test_linear_terminal(self):
        graph = SchemeGraph(_linear_scheme(["a", "b", "c"]))
        terminals = {n.id for n in graph.get_terminal_nodes()}
        assert terminals == {"c"}

    def test_diamond_single_terminal(self):
        scheme = SchemeDefinition(
            id="diamond_t",
            name="Diamond T",
            description="d",
            version="0.0.1",
            nodes=[_make_node("root"), _make_node("left"), _make_node("right"), _make_node("sink")],
            edges=[
                _make_edge("root", "left", condition="pass"),
                _make_edge("root", "right", condition="fail"),
                _make_edge("left", "sink"),
                _make_edge("right", "sink"),
            ],
        )
        terminals = {n.id for n in SchemeGraph(scheme).get_terminal_nodes()}
        assert terminals == {"sink"}

    def test_bugfix_terminals_are_create_pr_and_escalate(self):
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD

        graph = SchemeGraph(BUGFIX_STANDARD)
        terminals = {n.id for n in graph.get_terminal_nodes()}
        assert terminals == {"create_pr", "escalate"}

    def test_feature_terminals_are_create_pr_and_escalate(self):
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        graph = SchemeGraph(FEATURE_STANDARD)
        terminals = {n.id for n in graph.get_terminal_nodes()}
        assert terminals == {"create_pr", "escalate"}


# ---------------------------------------------------------------------------
# SchemeGraph.get_node()
# ---------------------------------------------------------------------------


class TestSchemeGraphGetNode:
    def test_returns_node_by_id(self):
        graph = SchemeGraph(_linear_scheme(["a", "b"]))
        node = graph.get_node("a")
        assert node is not None
        assert node.id == "a"

    def test_returns_none_for_unknown_id(self):
        graph = SchemeGraph(_linear_scheme(["a", "b"]))
        assert graph.get_node("z") is None


# ---------------------------------------------------------------------------
# SchemeRegistry
# ---------------------------------------------------------------------------


class TestSchemeRegistry:
    def setup_method(self):
        SchemeRegistry.clear()

    def teardown_method(self):
        SchemeRegistry.clear()

    def test_register_and_get(self):
        scheme = _linear_scheme(["a", "b"])
        SchemeRegistry.register(scheme)
        result = SchemeRegistry.get("linear_test")
        assert result is not None
        assert isinstance(result, SchemeGraph)

    def test_list_schemes(self):
        SchemeRegistry.register(_linear_scheme(["a", "b"]))
        assert "linear_test" in SchemeRegistry.list_schemes()

    def test_clear(self):
        SchemeRegistry.register(_linear_scheme(["a", "b"]))
        SchemeRegistry.clear()
        assert SchemeRegistry.list_schemes() == []

    def test_get_returns_none_for_unknown(self):
        assert SchemeRegistry.get("does_not_exist") is None

    def test_register_invalid_scheme_raises(self):
        # A scheme with an invalid edge reference should raise ValueError
        bad_scheme = SchemeDefinition(
            id="invalid_scheme",
            name="Invalid",
            description="d",
            version="0.0.1",
            nodes=[_make_node("a"), _make_node("b")],
            edges=[_make_edge("a", "ghost")],
        )
        with pytest.raises(ValueError, match="invalid"):
            SchemeRegistry.register(bad_scheme)

    def test_register_cyclic_scheme_raises(self):
        cyclic = SchemeDefinition(
            id="cyclic_reg",
            name="Cyclic Reg",
            description="d",
            version="0.0.1",
            nodes=[_make_node("a"), _make_node("b")],
            edges=[_make_edge("a", "b"), _make_edge("b", "a")],
        )
        with pytest.raises(ValueError):
            SchemeRegistry.register(cyclic)

    def test_bugfix_standard_auto_registered(self):
        # Re-import to trigger registration
        import importlib

        import henchmen.schemes.bugfix_standard  # noqa: F401

        importlib.reload(henchmen.schemes.bugfix_standard)
        assert "bugfix_standard" in SchemeRegistry.list_schemes()

    def test_feature_standard_auto_registered(self):
        import importlib

        import henchmen.schemes.feature_standard  # noqa: F401

        importlib.reload(henchmen.schemes.feature_standard)
        assert "feature_standard" in SchemeRegistry.list_schemes()

    def test_auto_discover_registers_all(self):
        SchemeRegistry.auto_discover()
        schemes = SchemeRegistry.list_schemes()
        assert "bugfix_standard" in schemes
        assert "feature_standard" in schemes


# ---------------------------------------------------------------------------
# Scheme edge conditions — retry nodes must have conditional edges
# ---------------------------------------------------------------------------


class TestSchemeEdgeConditions:
    """Test that retry nodes have proper conditional edges."""

    def test_bugfix_run_lint_retry_fail_goes_to_escalate(self):
        """run_lint_retry fail should escalate — only green PRs get created."""
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD

        graph = SchemeGraph(BUGFIX_STANDARD)
        fail_nexts = graph.get_next_nodes("run_lint_retry", condition="fail")
        assert len(fail_nexts) == 1
        assert fail_nexts[0].id == "escalate"

    def test_bugfix_run_lint_retry_pass_goes_to_run_tests(self):
        """run_lint_retry pass should proceed to run_tests."""
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD

        graph = SchemeGraph(BUGFIX_STANDARD)
        pass_nexts = graph.get_next_nodes("run_lint_retry", condition="pass")
        assert len(pass_nexts) == 1
        assert pass_nexts[0].id == "run_tests"

    def test_feature_run_lint_retry_fail_goes_to_escalate(self):
        """run_lint_retry fail should escalate — only green PRs get created."""
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        graph = SchemeGraph(FEATURE_STANDARD)
        fail_nexts = graph.get_next_nodes("run_lint_retry", condition="fail")
        assert len(fail_nexts) == 1
        assert fail_nexts[0].id == "escalate"

    def test_feature_run_lint_retry_pass_goes_to_run_tests(self):
        """run_lint_retry pass should proceed to run_tests in feature scheme."""
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        graph = SchemeGraph(FEATURE_STANDARD)
        pass_nexts = graph.get_next_nodes("run_lint_retry", condition="pass")
        assert len(pass_nexts) == 1
        assert pass_nexts[0].id == "run_tests"


# ---------------------------------------------------------------------------
# Per-node invariants enforced by SchemeGraph.validate()
# ---------------------------------------------------------------------------


def _one_node_scheme(node: SchemeNode) -> SchemeDefinition:
    return SchemeDefinition(
        id="one_node",
        name="One Node",
        description="d",
        version="0.0.1",
        nodes=[node],
        edges=[],
    )


class TestSchemeNodeInvariants:
    def test_agentic_node_without_instruction_template_is_invalid(self):
        node = SchemeNode(
            id="implement",
            name="Implement",
            node_type=NodeType.AGENTIC,
            model_name=ModelTier.COMPLEX.value,
        )
        errors = SchemeGraph(_one_node_scheme(node)).validate()
        assert any("instruction_template" in e for e in errors)

    def test_agentic_node_without_model_name_is_invalid(self):
        node = SchemeNode(
            id="implement",
            name="Implement",
            node_type=NodeType.AGENTIC,
            instruction_template="do the thing",
        )
        errors = SchemeGraph(_one_node_scheme(node)).validate()
        assert any("model_name" in e for e in errors)

    def test_agentic_node_with_blank_model_name_is_invalid(self):
        node = SchemeNode(
            id="implement",
            name="Implement",
            node_type=NodeType.AGENTIC,
            model_name="   ",
            instruction_template="do the thing",
        )
        errors = SchemeGraph(_one_node_scheme(node)).validate()
        assert any("model_name" in e for e in errors)

    @pytest.mark.parametrize("model_name", ["default/complx", "gemini-2.5-pro", "claude-sonnet-5"])
    def test_agentic_node_with_a_non_tier_model_name_is_invalid(self, model_name: str):
        """Tier typos and concrete vendor ids must fail at registration, not after a Lair starts."""
        node = SchemeNode(
            id="implement",
            name="Implement",
            node_type=NodeType.AGENTIC,
            model_name=model_name,
            instruction_template="do the thing",
        )
        errors = SchemeGraph(_one_node_scheme(node)).validate()
        assert any("not a model tier" in e for e in errors)

    def test_scheme_node_has_no_grounding_field(self):
        """Grounding had no consumer on the provider path; the field must not advertise it."""
        assert "grounding_enabled" not in SchemeNode.model_fields

    def test_deterministic_node_with_model_name_is_invalid(self):
        """A deterministic node that names a model is the fix_lint regression."""
        node = SchemeNode(
            id="run_lint",
            name="Run Lint",
            node_type=NodeType.DETERMINISTIC,
            model_name=ModelTier.LIGHT.value,
        )
        errors = SchemeGraph(_one_node_scheme(node)).validate()
        assert any("must not set model_name" in e for e in errors)

    def test_deterministic_node_with_instruction_template_is_invalid(self):
        node = SchemeNode(
            id="run_lint",
            name="Run Lint",
            node_type=NodeType.DETERMINISTIC,
            instruction_template="You are fixing lint errors.",
        )
        errors = SchemeGraph(_one_node_scheme(node)).validate()
        assert any("must not set instruction_template" in e for e in errors)

    def test_duplicate_node_ids_are_invalid(self):
        scheme = SchemeDefinition(
            id="dupes",
            name="Dupes",
            description="d",
            version="0.0.1",
            nodes=[_make_node("a"), _make_node("a"), _make_node("b")],
            edges=[_make_edge("a", "b")],
        )
        errors = SchemeGraph(scheme).validate()
        assert any("Duplicate node id" in e for e in errors)

    def test_valid_deterministic_node_passes(self):
        node = SchemeNode(id="run_lint", name="Run Lint", node_type=NodeType.DETERMINISTIC)
        assert SchemeGraph(_one_node_scheme(node)).validate() == []


# ---------------------------------------------------------------------------
# Contracts every registered scheme must satisfy
# ---------------------------------------------------------------------------


def _registered_schemes() -> list[SchemeDefinition]:
    from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD
    from henchmen.schemes.feature_standard import FEATURE_STANDARD
    from henchmen.schemes.goal_decomposition import GOAL_DECOMPOSITION

    return [BUGFIX_STANDARD, FEATURE_STANDARD, GOAL_DECOMPOSITION]


def _all_nodes() -> list[tuple[str, SchemeNode]]:
    return [(scheme.id, node) for scheme in _registered_schemes() for node in scheme.nodes]


def _node_case_id(value: Any) -> str:
    return value.id if isinstance(value, SchemeNode) else str(value)


def _arsenal_tool_categories() -> set[str]:
    """Tool categories the Arsenal implements — one module per category.

    Read from the package directory rather than from ``ToolRegistry``: the
    registry is process-global mutable state that other test modules clear, and
    re-importing an already-imported module does not re-register anything.
    """
    import henchmen

    tools_dir = Path(henchmen.__path__[0]) / "arsenal" / "tools"
    return {path.stem for path in tools_dir.glob("*.py") if not path.stem.startswith("_")}


class TestRegisteredSchemeContracts:
    """The shipped schemes are static code - pin the contracts they must honour."""

    @pytest.mark.parametrize("scheme", _registered_schemes(), ids=lambda s: s.id)
    def test_scheme_validates(self, scheme: SchemeDefinition):
        assert SchemeGraph(scheme).validate() == []

    @pytest.mark.parametrize("scheme_id,node", _all_nodes(), ids=_node_case_id)
    def test_agentic_nodes_use_a_model_tier(self, scheme_id: str, node: SchemeNode):
        """A concrete model id or a typo would only surface as a provider 404 at runtime."""
        if node.node_type != NodeType.AGENTIC:
            pytest.skip("deterministic node")
        assert node.model_name in {tier.value for tier in ModelTier}, (
            f"{scheme_id}.{node.id} uses model_name={node.model_name!r}; expected a ModelTier value"
        )

    @pytest.mark.parametrize("scheme_id,node", _all_nodes(), ids=_node_case_id)
    def test_deterministic_nodes_carry_no_llm_config(self, scheme_id: str, node: SchemeNode):
        if node.node_type != NodeType.DETERMINISTIC:
            pytest.skip("agentic node")
        assert node.model_name is None
        assert node.instruction_template is None

    @pytest.mark.parametrize("scheme_id,node", _all_nodes(), ids=_node_case_id)
    def test_deterministic_nodes_have_a_registered_handler(self, scheme_id: str, node: SchemeNode):
        """Without a handler the executor falls through and the node is a silent no-op."""
        if node.node_type != NodeType.DETERMINISTIC:
            pytest.skip("agentic node")
        from henchmen.mastermind.scheme_executor.handlers import get_handler

        assert get_handler(node.id) is not None or get_handler(node.name) is not None, (
            f"{scheme_id}.{node.id} is deterministic but no handler is registered for it"
        )

    @pytest.mark.parametrize("scheme_id,node", _all_nodes(), ids=_node_case_id)
    def test_arsenal_tool_sets_are_real_categories(self, scheme_id: str, node: SchemeNode):
        """An unknown category yields an empty tool list with no error at runtime."""
        if node.arsenal_requirement is None:
            pytest.skip("no arsenal requirement")
        implemented = _arsenal_tool_categories()
        for tool_set in node.arsenal_requirement.tool_sets:
            assert tool_set in ARSENAL_TOOL_SETS
            assert tool_set in implemented, f"{scheme_id}.{node.id} requires unimplemented tool set {tool_set!r}"


class TestFixLintIsDeterministic:
    """CLAUDE.md and the cost model promise fix_lint costs zero LLM tokens."""

    @pytest.mark.parametrize("scheme", _registered_schemes()[:2], ids=lambda s: s.id)
    def test_fix_lint_node_type(self, scheme: SchemeDefinition):
        node = next(n for n in scheme.nodes if n.id == "fix_lint")
        assert node.node_type == NodeType.DETERMINISTIC
        assert node.model_name is None
        assert node.instruction_template is None

    def test_fix_lint_handler_is_registered(self):
        from henchmen.mastermind.scheme_executor.handlers import get_handler

        assert get_handler("fix_lint") is not None


class TestSchemeDeduplication:
    """bugfix_standard and feature_standard share one pipeline definition."""

    def test_shared_nodes_are_identical(self):
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        bugfix = {n.id: n for n in BUGFIX_STANDARD.nodes}
        feature = {n.id: n for n in FEATURE_STANDARD.nodes}
        shared_ids = (set(bugfix) & set(feature)) - {"implement_fix", "implement_feature"}
        assert "fix_tests" in shared_ids
        assert "fix_lint" in shared_ids
        for node_id in shared_ids:
            assert bugfix[node_id] == feature[node_id], f"node '{node_id}' drifted between the two schemes"

    def test_shared_nodes_are_distinct_objects(self):
        """Each scheme owns its own node instances - the models are mutable."""
        from henchmen.schemes.bugfix_standard import BUGFIX_STANDARD
        from henchmen.schemes.feature_standard import FEATURE_STANDARD

        bugfix = next(n for n in BUGFIX_STANDARD.nodes if n.id == "fix_tests")
        feature = next(n for n in FEATURE_STANDARD.nodes if n.id == "fix_tests")
        assert bugfix is not feature

    def test_analyze_goal_uses_the_shared_plan_template(self):
        from henchmen.schemes._shared_templates import PLAN_INSTRUCTION_TEMPLATE
        from henchmen.schemes.goal_decomposition import GOAL_DECOMPOSITION

        node = next(n for n in GOAL_DECOMPOSITION.nodes if n.id == "analyze_goal")
        assert node.instruction_template == PLAN_INSTRUCTION_TEMPLATE


class TestGoalDecomposition:
    def test_is_valid(self):
        from henchmen.schemes.goal_decomposition import GOAL_DECOMPOSITION

        assert SchemeGraph(GOAL_DECOMPOSITION).validate() == []

    def test_root_is_analyze_goal(self):
        from henchmen.schemes.goal_decomposition import GOAL_DECOMPOSITION

        assert SchemeGraph(GOAL_DECOMPOSITION).get_root_node().id == "analyze_goal"

    def test_terminal_is_report_plan(self):
        from henchmen.schemes.goal_decomposition import GOAL_DECOMPOSITION

        terminals = {n.id for n in SchemeGraph(GOAL_DECOMPOSITION).get_terminal_nodes()}
        assert terminals == {"report_plan"}

    def test_auto_discover_registers_goal_decomposition(self):
        SchemeRegistry.auto_discover()
        assert "goal_decomposition" in SchemeRegistry.list_schemes()


# ---------------------------------------------------------------------------
# Step budgets
# ---------------------------------------------------------------------------


class TestStepBudgets:
    def test_defaults_only_key_agentic_nodes(self):
        """A budget keyed on a deterministic node id can never be used - it is drift."""
        agentic_ids = {node.id for _scheme_id, node in _all_nodes() if node.node_type == NodeType.AGENTIC}
        assert set(STEP_BUDGET_DEFAULTS) <= agentic_ids, (
            f"STEP_BUDGET_DEFAULTS keys not agentic nodes: {set(STEP_BUDGET_DEFAULTS) - agentic_ids}"
        )

    def test_every_agentic_node_has_a_default_budget(self):
        agentic_ids = {node.id for _scheme_id, node in _all_nodes() if node.node_type == NodeType.AGENTIC}
        assert agentic_ids <= set(STEP_BUDGET_DEFAULTS)

    def test_base_steps_may_not_exceed_max_steps(self):
        with pytest.raises(ValueError, match="max_steps"):
            StepBudget(base_steps=40, max_steps=10)

    def test_zero_steps_rejected(self):
        with pytest.raises(ValueError):
            StepBudget(base_steps=0)


# ---------------------------------------------------------------------------
# Tier plumbing: a tier name must resolve to a priced, concrete model
# ---------------------------------------------------------------------------


class TestTierResolution:
    @pytest.mark.parametrize("tier", list(ModelTier), ids=lambda t: t.value)
    def test_tier_resolves_to_a_concrete_model(self, tier: ModelTier, mock_settings):
        from henchmen.providers.tiers import is_tier_name, resolve_model_name

        resolved = resolve_model_name(mock_settings, tier.value)
        assert resolved
        assert not is_tier_name(resolved)

    @pytest.mark.parametrize("tier", list(ModelTier), ids=lambda t: t.value)
    @pytest.mark.parametrize("provider", ["anthropic", "openai", "gcp", "aws"])
    def test_tier_has_a_nonzero_price(self, tier: ModelTier, provider: str, mock_settings):
        """The pre-dispatch cost gate is useless if a tier name prices at $0.

        The provider is named explicitly. Ollama models are free by design, so
        leaving it to ambient configuration would make this assertion pass or
        fail on whether a developer happened to have Ollama selected.
        """
        from henchmen.providers.pricing import estimate_cost_for_settings

        settings = mock_settings.model_copy(update={"llm_provider": provider})
        cost = estimate_cost_for_settings(settings, tier.value, 100_000, 10_000)
        assert cost > 0

    def test_scheme_node_model_names_resolve(self, mock_settings):
        from henchmen.providers.tiers import resolve_model_name

        for scheme_id, node in _all_nodes():
            if node.node_type != NodeType.AGENTIC or node.model_name is None:
                continue
            assert resolve_model_name(mock_settings, node.model_name), f"{scheme_id}.{node.id} resolves to nothing"


# ---------------------------------------------------------------------------
# Shared model contracts (models/scheme.py, models/operative.py, models/task.py)
# ---------------------------------------------------------------------------


class TestArsenalRequirement:
    def test_rejects_unknown_tool_set(self):
        with pytest.raises(ValueError):
            ArsenalRequirement(tool_sets=["gcp"])

    def test_known_tool_sets_match_the_implemented_categories(self):
        """The Literal must stay in lockstep with henchmen.arsenal.tools."""
        assert _arsenal_tool_categories() == ARSENAL_TOOL_SETS


class TestOperativeConfigDefaults:
    def test_model_name_defaults_to_the_complex_tier(self):
        from henchmen.models.operative import OperativeConfig

        config = OperativeConfig(task_id="t", node_id="n", scheme_id="s")
        assert config.model_name == ModelTier.COMPLEX.value


class TestTimezoneAwareDatetimes:
    def test_task_rejects_naive_created_at(self):
        from henchmen.models.task import HenchmenTask, TaskContext, TaskSource

        with pytest.raises(ValueError):
            HenchmenTask(
                source=TaskSource.CLI,
                source_id="1",
                title="t",
                description="d",
                context=TaskContext(repo="o/r"),
                created_by="me",
                created_at=datetime(2026, 1, 1, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
            )

    def test_task_accepts_aware_created_at(self):
        from henchmen.models.task import HenchmenTask, TaskContext, TaskSource

        task = HenchmenTask(
            source=TaskSource.CLI,
            source_id="1",
            title="t",
            description="d",
            context=TaskContext(repo="o/r"),
            created_by="me",
            created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        )
        assert task.created_at.tzinfo is not None

    def test_report_rejects_naive_started_at(self):
        from henchmen.models.operative import OperativeReport, OperativeStatus

        with pytest.raises(ValueError):
            OperativeReport(
                task_id="t",
                scheme_id="s",
                node_id="n",
                operative_id="o",
                status=OperativeStatus.COMPLETED,
                summary="done",
                confidence_score=1.0,
                started_at=datetime(2026, 1, 1, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
            )

    def test_report_rejects_naive_completed_at(self):
        from henchmen.models.operative import OperativeReport, OperativeStatus

        with pytest.raises(ValueError):
            OperativeReport(
                task_id="t",
                scheme_id="s",
                node_id="n",
                operative_id="o",
                status=OperativeStatus.COMPLETED,
                summary="done",
                confidence_score=1.0,
                started_at=datetime.now(UTC),
                completed_at=datetime(2026, 1, 1, 12, 0, 0),  # noqa: DTZ001 - deliberately naive
            )
