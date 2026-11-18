class CycleError(ValueError):
    """The dependency graph contains a cycle."""


def toposort(graph: dict[str, list[str]]) -> list[str]:
    """Order the nodes so that every node follows its dependencies."""
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(node: str) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            raise CycleError(node)
        state[node] = 1
        for dep in graph.get(node, []):
            visit(dep)
        state[node] = 2
        order.append(node)

    for node in graph:
        visit(node)
    return order
