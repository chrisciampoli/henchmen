"""Unit tests for SchemeGraph and SchemeRegistry."""

import pytest

from henchmen.models.scheme import (
    NodeType,
    SchemeDefinition,
    SchemeEdge,
    SchemeNode,
)
from henchmen.schemes.base import SchemeGraph
from henchmen.schemes.registry import SchemeRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(node_id: str, node_type: NodeType = NodeType.DETERMINISTIC) -> SchemeNode:
    return SchemeNode(id=node_id, name=node_id.replace("_", " ").title(), node_type=node_type)


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
