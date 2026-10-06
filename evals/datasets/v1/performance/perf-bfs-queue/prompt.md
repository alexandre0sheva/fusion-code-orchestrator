`bfs_order(graph, start)` returns the nodes reachable from `start` in breadth-first order. `graph` maps each node to the list of its neighbours; neighbours are visited in the order listed, each node once. A node missing from `graph` has no neighbours.

```python
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
```

Make `bfs_order` fast for large inputs (graphs of 10^5 nodes, some with very wide levels) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
