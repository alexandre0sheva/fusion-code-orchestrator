def range_sums(values, queries):
    """The sum of values[l:r] for each (l, r) in ``queries``, bounds clamped, empty ranges 0."""
    answers = []
    for left, right in queries:
        left = max(left, 0)
        right = min(right, len(values))
        answers.append(sum(values[left:right]) if left < right else 0)
    return answers
