def max_subarray_sum(values):
    """Largest sum of a non-empty contiguous run of ``values``."""
    if not values:
        raise ValueError("values must not be empty")
    best = values[0]
    for start in range(len(values)):
        total = 0
        for end in range(start, len(values)):
            total += values[end]
            if total > best:
                best = total
    return best
