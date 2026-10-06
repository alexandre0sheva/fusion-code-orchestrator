`render_newest_first(lines)` formats a log for display, newest line first. Each line is shown as its 1-based position in the input, a tab, and the text; entries are joined with newlines and there is no trailing newline. No lines give an empty string.

```python
def render_newest_first(lines):
    """One string, newest line first, each as "<position>\t<text>"."""
    text = ""
    for position, line in enumerate(lines, 1):
        entry = f"{position}\t{line}"
        text = entry + ("\n" + text if text else "")
    return text
```

Make `render_newest_first` fast for large inputs (logs of 10^5 lines) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
