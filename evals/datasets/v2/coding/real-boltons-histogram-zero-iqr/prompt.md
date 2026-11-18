Bug report for this repository (the `boltons` package, `statsutils` module): histograms crash
when the interquartile range is zero.

`Stats([5] * 10).get_histogram_counts()` raises `ZeroDivisionError`, and so does
`Stats([0] * 10 + [100]).get_histogram_counts()`: the default bin selection (Freedman-Diaconis)
divides by a bin width of zero when the 25th and 75th percentiles are equal. In that case the
histogram should have a single bin starting at the minimum and holding every value
(`[(5.0, 10)]` and `[(0.0, 11)]`), and `format_histogram()` should work. Data with a non-zero
interquartile range must keep its current bins.

Fix the bin selection in `Stats`. The package's existing tests are in `tests/`.
