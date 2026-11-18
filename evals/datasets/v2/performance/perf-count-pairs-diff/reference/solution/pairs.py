def count_pairs(nums, diff):
    """Number of index pairs i < j with nums[j] - nums[i] == diff."""
    seen = {}
    count = 0
    for value in nums:
        count += seen.get(value - diff, 0)
        seen[value] = seen.get(value, 0) + 1
    return count
