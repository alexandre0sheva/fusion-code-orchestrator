from collections import deque


def bfs_order(graph, start):
    """Nodes reachable from ``start`` in breadth-first order."""
    order = []
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        order.append(node)
        for neighbour in graph.get(node, []):
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)
    return order
