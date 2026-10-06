def unique(items):
    """Return the distinct items of ``items``, in order of first appearance."""
    return list(dict.fromkeys(items))
