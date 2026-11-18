def two_sum(nums, target):
    """Indices (i, j), i < j, of the first pair adding up to ``target``, or None."""
    last_seen = {}
    for j, value in enumerate(nums):
        i = last_seen.get(target - value)
        if i is not None:
            return (i, j)
        last_seen[value] = j
    return None
