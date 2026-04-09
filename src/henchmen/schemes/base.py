"""SchemeGraph - wraps SchemeDefinition with DAG operations."""

from collections import defaultdict, deque

from henchmen.models.scheme import SchemeDefinition, SchemeEdge, SchemeNode


class SchemeGraph:
    """Wraps a SchemeDefinition with DAG operations."""

    def __init__(self, definition: SchemeDefinition):
        self.definition = definition
        self._node_map: dict[str, SchemeNode] = {n.id: n for n in definition.nodes}
        self._adjacency: dict[str, list[SchemeEdge]] = {}  # from_node -> edges
        self._build_adjacency()

    def _build_adjacency(self) -> None:
        """Build adjacency list from edges."""
        for node_id in self._node_map:
            self._adjacency[node_id] = []
        for edge in self.definition.edges:
            self._adjacency.setdefault(edge.from_node, []).append(edge)

    def validate(self) -> list[str]:
        """Validate the DAG: no cycles, all edge references valid, exactly one root node, all nodes reachable.

        Returns list of error strings, empty if valid.
        """
        errors: list[str] = []

        # Check all edge node references are valid
        for edge in self.definition.edges:
            if edge.from_node not in self._node_map:
                errors.append(f"Edge references unknown from_node: '{edge.from_node}'")
            if edge.to_node not in self._node_map:
                errors.append(f"Edge references unknown to_node: '{edge.to_node}'")

        # If there are bad references, further graph checks are unreliable
        if errors:
            return errors

        # Find nodes with incoming edges
        nodes_with_incoming: set[str] = set()
        for edge in self.definition.edges:
            nodes_with_incoming.add(edge.to_node)

        root_nodes = [nid for nid in self._node_map if nid not in nodes_with_incoming]

        if len(root_nodes) == 0:
            errors.append("No root node found (every node has an incoming edge — cycle?)")
        elif len(root_nodes) > 1:
            errors.append(f"Multiple root nodes found: {sorted(root_nodes)}")

        # Cycle detection via DFS — only flag cycles where ALL edges involved are unconditional.
        # Cycles involving at least one conditional edge are "controlled loops" (e.g. verify→implement
        # retry loops) and are allowed. The SchemeExecutor's retry limit prevents infinite execution.
        white, gray, black = 0, 1, 2
        color: dict[str, int] = {nid: white for nid in self._node_map}

        def dfs_has_unconditional_cycle(node_id: str) -> bool:
            color[node_id] = gray
            for edge in self._adjacency.get(node_id, []):
                if edge.condition is not None:
                    # Conditional edge — skip; conditional back-edges are allowed (controlled loops)
                    continue
                neighbor = edge.to_node
                if color[neighbor] == gray:
                    return True
                if color[neighbor] == white and dfs_has_unconditional_cycle(neighbor):
                    return True
            color[node_id] = black
            return False

        for node_id in self._node_map:
            if color[node_id] == white:
                if dfs_has_unconditional_cycle(node_id):
                    errors.append("Cycle detected in scheme graph")
                    break

        # Check all nodes are reachable from the root (only if exactly one root)
        if len(root_nodes) == 1:
            root_id = root_nodes[0]
            visited: set[str] = set()
            queue: deque[str] = deque([root_id])
            while queue:
                current = queue.popleft()
                if current in visited:
                    continue
                visited.add(current)
                for edge in self._adjacency.get(current, []):
                    if edge.to_node not in visited:
                        queue.append(edge.to_node)

            unreachable = set(self._node_map) - visited
            if unreachable:
                errors.append(f"Unreachable nodes from root '{root_id}': {sorted(unreachable)}")

        return errors

    def get_node(self, node_id: str) -> SchemeNode | None:
        """Return the SchemeNode with the given ID, or None if not found."""
        return self._node_map.get(node_id)

    def get_root_node(self) -> SchemeNode:
        """Return the entry node (node with no incoming edges).

        Raises ValueError if the graph has no valid single root.
        """
        nodes_with_incoming: set[str] = {edge.to_node for edge in self.definition.edges}
        roots = [n for n in self.definition.nodes if n.id not in nodes_with_incoming]
        if len(roots) != 1:
            raise ValueError(f"Expected exactly one root node, found {len(roots)}: {[r.id for r in roots]}")
        return roots[0]

    def get_next_nodes(self, node_id: str, condition: str | None = None) -> list[SchemeNode]:
        """Get successor nodes, optionally filtered by edge condition ('pass'/'fail'/None).

        When condition is None, returns nodes reachable via unconditional edges only.
        When condition is 'pass' or 'fail', returns nodes reachable via matching conditional edges.
        """
        edges = self._adjacency.get(node_id, [])
        result: list[SchemeNode] = []
        for edge in edges:
            if edge.condition == condition:
                node = self._node_map.get(edge.to_node)
                if node is not None:
                    result.append(node)
        return result

    def topological_sort(self) -> list[SchemeNode]:
        """Return nodes in topological order using Kahn's algorithm.

        Raises ValueError if a cycle is detected.
        """
        in_degree: dict[str, int] = defaultdict(int)
        for node_id in self._node_map:
            in_degree.setdefault(node_id, 0)
        for edge in self.definition.edges:
            in_degree[edge.to_node] += 1

        queue: deque[str] = deque(nid for nid in self._node_map if in_degree[nid] == 0)
        sorted_nodes: list[SchemeNode] = []

        while queue:
            node_id = queue.popleft()
            sorted_nodes.append(self._node_map[node_id])
            for edge in self._adjacency.get(node_id, []):
                in_degree[edge.to_node] -= 1
                if in_degree[edge.to_node] == 0:
                    queue.append(edge.to_node)

        if len(sorted_nodes) != len(self._node_map):
            raise ValueError("Cycle detected — topological sort is not possible")

        return sorted_nodes

    def get_terminal_nodes(self) -> list[SchemeNode]:
        """Return nodes with no outgoing edges."""
        return [self._node_map[node_id] for node_id in self._node_map if not self._adjacency.get(node_id)]
