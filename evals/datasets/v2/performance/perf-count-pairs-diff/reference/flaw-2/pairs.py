from collections import Counter


def count_pairs(nums, diff):
    """Number of index pairs i < j with nums[j] - nums[i] == diff."""
    counts = Counter(nums)
    return sum(n * counts.get(value + diff, 0) for value, n in counts.items())
