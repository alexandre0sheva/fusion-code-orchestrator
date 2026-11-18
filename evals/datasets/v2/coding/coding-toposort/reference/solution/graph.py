import heapq


class CycleError(ValueError):
    """The dependency graph contains a cycle."""


def toposort(graph: dict[str, list[str]]) -> list[str]:
    """Order the nodes so that every node follows its dependencies."""
    deps: dict[str, set[str]] = {node: set(requires) for node, requires in graph.items()}
    for requires in list(deps.values()):
        for node in requires:
            deps.setdefault(node, set())
    waiting = {node: len(requires) for node, requires in deps.items()}
    dependents: dict[str, list[str]] = {node: [] for node in deps}
    for node, requires in deps.items():
        for required in requires:
            dependents[required].append(node)
    ready = [node for node, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for dependent in dependents[node]:
            waiting[dependent] -= 1
            if waiting[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(order) != len(deps):
        stuck = sorted(set(deps) - set(order))
        raise CycleError(f"dependency cycle involving: {', '.join(stuck)}")
    return order
