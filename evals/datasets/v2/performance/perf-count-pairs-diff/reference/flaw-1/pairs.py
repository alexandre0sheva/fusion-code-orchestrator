def count_pairs(nums, diff):
    """Number of index pairs i < j with nums[j] - nums[i] == diff."""
    count = 0
    for j, value in enumerate(nums):
        count += nums[:j].count(value - diff)
    return count
