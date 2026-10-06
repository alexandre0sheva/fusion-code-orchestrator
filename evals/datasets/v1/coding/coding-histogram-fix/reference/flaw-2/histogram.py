def histogram(values, bins, lo=None, hi=None):
    if bins < 1:
        raise ValueError("bins must be at least 1")
    values = list(values)
    counts = [0] * bins
    if not values:
        return counts
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    if lo > hi:
        raise ValueError("lo must not exceed hi")
    if lo == hi:
        counts[0] = sum(1 for value in values if value == lo)
        return counts
    width = (hi - lo) / bins
    for value in values:
        if lo <= value < hi:
            counts[int((value - lo) / width)] += 1
    return counts
