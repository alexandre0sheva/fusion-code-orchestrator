`histogram(values, bins, lo=None, hi=None)` in `histogram.py` counts values into equal-width bins, but it raises `IndexError` for the largest value and `ZeroDivisionError` when all values are equal, and it counts values outside the range.

Fix it so that it returns a list of `bins` counts:

- `bins` must be at least 1, else `ValueError`. `lo` and `hi` default to the minimum and maximum of `values`; `lo > hi` raises `ValueError`.
- Bin `i` covers `[lo + i*w, lo + (i+1)*w)` where `w = (hi - lo) / bins`; the **last bin also includes `hi`**.
- Values below `lo` or above `hi` are ignored.
- No values gives `[0] * bins`. If `lo == hi`, every value equal to `lo` goes into the first bin.
