def count_inversions(values):
    """Number of pairs i < j with values[i] > values[j]."""
    return _sort_count(list(values))[1]


def _sort_count(items):
    if len(items) < 2:
        return items, 0
    middle = len(items) // 2
    left, left_count = _sort_count(items[:middle])
    right, right_count = _sort_count(items[middle:])
    merged = []
    count = left_count + right_count
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i] < right[j]:
            merged.append(left[i])
            i += 1
        else:
            merged.append(right[j])
            count += len(left) - i
            j += 1
    merged.extend(left[i:])
    merged.extend(right[j:])
    return merged, count
