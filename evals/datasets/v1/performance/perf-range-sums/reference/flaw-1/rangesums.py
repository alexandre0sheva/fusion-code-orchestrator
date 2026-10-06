def range_sums(values, queries):
    """The sum of values[l:r] for each (l, r) in ``queries``, bounds clamped, empty ranges 0."""
    answers = []
    for left, right in queries:
        total = 0
        for index in range(max(left, 0), min(right, len(values))):
            total += values[index]
        answers.append(total)
    return answers
