def max_subarray_sum(values):
    """Largest sum of a non-empty contiguous run of ``values``."""
    if not values:
        raise ValueError("values must not be empty")
    best = running = 0
    for value in values:
        running = max(0, running + value)
        best = max(best, running)
    return best
