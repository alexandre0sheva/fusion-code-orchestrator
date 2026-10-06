def intersect_sorted(a, b):
    """Values present in both sorted lists, each once, ascending."""
    common = []
    for value in a:
        if value in b and value not in common:
            common.append(value)
    return common
