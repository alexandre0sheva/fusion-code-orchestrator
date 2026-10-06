def unique(items):
    """Return the distinct items of ``items``, in order of first appearance."""
    result = []
    for item in items:
        if item not in result:
            result.append(item)
    return result
