from itertools import accumulate


def range_sums(values, queries):
    """The sum of values[l:r] for each (l, r) in ``queries``, bounds clamped, empty ranges 0."""
    prefix = [0, *accumulate(values)]
    return [prefix[right] - prefix[left] for left, right in queries]
