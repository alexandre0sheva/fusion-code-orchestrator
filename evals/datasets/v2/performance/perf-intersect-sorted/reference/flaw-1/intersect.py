def intersect_sorted(a, b):
    """Values present in both sorted lists, each once, ascending."""
    return sorted({value for value in a if value in b})
