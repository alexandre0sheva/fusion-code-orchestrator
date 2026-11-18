def count_inversions(values):
    """Number of pairs i < j with values[i] > values[j]."""
    return sum(
        1
        for i in range(len(values))
        for j in range(i + 1, len(values))
        if values[i] > values[j]
    )
