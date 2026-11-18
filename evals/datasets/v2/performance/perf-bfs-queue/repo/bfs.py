def bfs_order(graph, start):
    """Nodes reachable from ``start`` in breadth-first order."""
    order = []
    seen = {start}
    queue = [start]
    while queue:
        node = queue.pop(0)
        order.append(node)
        for neighbour in graph.get(node, []):
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)
    return order
