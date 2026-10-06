`diffstat(diff_text)` in `diffstat.py` summarises a unified diff, but it counts the `---` and `+++` file header lines as removed and added lines, and it gets confused by changed lines whose text starts with `--` or `++` (for example a removed SQL comment `-- note`, which appears in the diff as `--- note`).

Fix it so that it returns `{"files": F, "added": A, "removed": R}`:

- `A` and `R` count only the `+` and `-` lines **inside hunks**. A hunk starts at a line `@@ -a,b +c,d @@` (a missing count means 1) and consists of exactly `b` old-side lines (context and `-`) and `d` new-side lines (context and `+`). Lines starting with `\` ("No newline at end of file") are not counted.
- File header lines (`--- a/x`, `+++ b/x`) and other lines outside hunks (`diff --git`, `index`) count for nothing.
- `F` is the number of distinct files in the diff. A file's name is the path of its `+++` line without the `b/` prefix; for a deleted file (`+++ /dev/null`) it is the `---` path without `a/`.
- An empty diff gives all zeros.
