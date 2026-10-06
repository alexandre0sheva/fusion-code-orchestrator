def histogram(values, bins, lo=None, hi=None):
    """Count ``values`` into ``bins`` equal-width bins between ``lo`` and ``hi``."""
    values = list(values)
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    width = (hi - lo) / bins
    counts = [0] * bins
    for value in values:
        counts[int((value - lo) / width)] += 1
    return counts
