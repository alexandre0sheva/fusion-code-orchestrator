def histogram(values, bins, lo=None, hi=None):
    """Count ``values`` into ``bins`` equal-width bins between ``lo`` and ``hi``."""
    if bins < 1:
        raise ValueError("bins must be at least 1")
    values = list(values)
    counts = [0] * bins
    if not values:
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("lo must not exceed hi")
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
        if value < lo or value > hi:
            continue
        counts[min(int((value - lo) / width), bins - 1)] += 1
    return counts
