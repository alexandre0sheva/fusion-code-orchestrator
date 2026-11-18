def bfs_order(graph, start):
    """Nodes reachable from ``start`` in breadth-first order."""
    order = []
    seen = {start}
    stack = [start]
    while stack:
        node = stack.pop()
        order.append(node)
        for neighbour in graph.get(node, []):
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return order
