Implement `merge_intervals(intervals)` in `intervals.py`.

`intervals` is a list of `(start, end)` integer tuples in any order. Return a new sorted list of tuples in which every group of intervals that overlap **or touch** (the next start is `<=` the current end) is merged into one. For example `[(1, 3), (2, 6), (8, 10), (6, 7)]` becomes `[(1, 7), (8, 10)]`.

- `[]` returns `[]`.
- An interval nested inside another disappears into it.
- `(1, 2)` and `(3, 4)` do not touch (integers), so they stay separate.
- The input list must not be modified.
- An interval whose start is greater than its end raises `ValueError`.
