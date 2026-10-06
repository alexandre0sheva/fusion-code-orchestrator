def intersect_sorted(a, b):
    """Values present in both sorted lists, each once, ascending."""
    common = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] < b[j]:
            i += 1
        elif a[i] > b[j]:
            j += 1
        else:
            if not common or common[-1] != a[i]:
                common.append(a[i])
            i += 1
            j += 1
    return common
