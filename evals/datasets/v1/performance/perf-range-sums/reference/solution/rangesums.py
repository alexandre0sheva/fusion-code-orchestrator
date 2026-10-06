from itertools import accumulate


def range_sums(values, queries):
    """The sum of values[l:r] for each (l, r) in ``queries``, bounds clamped, empty ranges 0."""
    prefix = [0, *accumulate(values)]
    size = len(values)
    answers = []
    for left, right in queries:
        left = max(left, 0)
        right = min(right, size)
        answers.append(prefix[right] - prefix[left] if left < right else 0)
    return answers
