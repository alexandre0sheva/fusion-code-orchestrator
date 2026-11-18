def count_pairs(nums, diff):
    """Number of index pairs i < j with nums[j] - nums[i] == diff."""
    count = 0
    for j in range(len(nums)):
        for i in range(j):
            if nums[j] - nums[i] == diff:
                count += 1
    return count
