def two_sum(nums, target):
    """Indices (i, j), i < j, of the first pair adding up to ``target``, or None."""
    for j in range(len(nums)):
        for i in range(j):
            if nums[i] + nums[j] == target:
                return (i, j)
    return None
