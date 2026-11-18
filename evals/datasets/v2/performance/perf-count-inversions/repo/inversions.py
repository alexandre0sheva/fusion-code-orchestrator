def count_inversions(values):
    """Number of pairs i < j with values[i] > values[j]."""
    count = 0
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            if values[i] > values[j]:
                count += 1
    return count
