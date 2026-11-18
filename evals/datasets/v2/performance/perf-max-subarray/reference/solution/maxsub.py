def max_subarray_sum(values):
    """Largest sum of a non-empty contiguous run of ``values``."""
    if not values:
        raise ValueError("values must not be empty")
    best = running = values[0]
    for value in values[1:]:
        running = max(value, running + value)
        best = max(best, running)
    return best
