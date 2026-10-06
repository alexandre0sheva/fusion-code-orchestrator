def histogram(values, bins, lo=None, hi=None):
    if bins < 1:
        raise ValueError("bins must be at least 1")
    values = list(values)
    if not values:
        return [0] * bins
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    width = (hi - lo) / bins
    counts = [0] * bins
    for value in values:
        index = min(max(int((value - lo) / width), 0), bins - 1)
        counts[index] += 1
    return counts
