def two_sum(nums, target):
    """Indices (i, j), i < j, of the first pair adding up to ``target``, or None."""
    for j, value in enumerate(nums):
        earlier = nums[:j]
        if target - value in earlier:
            return (earlier.index(target - value), j)
    return None
