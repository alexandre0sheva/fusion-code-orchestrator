def unique(items):
    """Return the distinct items of ``items``, in order of first appearance."""
    items = list(items)
    return [item for index, item in enumerate(items) if item not in items[:index]]
