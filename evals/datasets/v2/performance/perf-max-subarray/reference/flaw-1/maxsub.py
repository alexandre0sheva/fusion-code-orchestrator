def max_subarray_sum(values):
    """Largest sum of a non-empty contiguous run of ``values``."""
    if not values:
        raise ValueError("values must not be empty")
    prefix = [0]
    for value in values:
        prefix.append(prefix[-1] + value)
    return max(
        prefix[end] - prefix[start]
        for start in range(len(values))
        for end in range(start + 1, len(values) + 1)
    )
