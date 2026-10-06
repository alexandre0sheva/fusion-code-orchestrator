class CycleError(ValueError):
    """The dependency graph contains a cycle."""


def toposort(graph: dict[str, list[str]]) -> list[str]:
    """Order the nodes so that every node follows its dependencies."""
    raise NotImplementedError
