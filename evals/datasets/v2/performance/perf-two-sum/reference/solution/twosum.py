def two_sum(nums, target):
    """Indices (i, j), i < j, of the first pair adding up to ``target``, or None."""
    first_seen = {}
    for j, value in enumerate(nums):
        i = first_seen.get(target - value)
        if i is not None:
            return (i, j)
        first_seen.setdefault(value, j)
    return None
