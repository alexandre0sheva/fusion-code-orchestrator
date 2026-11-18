Implement `wrap(text, width)` in `wrapping.py`. It returns a list of lines, none longer than `width`.

- Paragraphs are separated by one or more blank lines (a line holding only whitespace is blank). Between two paragraphs the result has one empty string `""`. Leading and trailing blank lines are ignored. Whitespace-only or empty text gives `[]`.
- Inside a paragraph, any run of whitespace (including single newlines) is one space. Words are filled greedily: a word joins the current line when the line plus one space plus the word fits in `width`, otherwise it starts a new line.
- A word longer than `width` is split into chunks of `width` characters (the last one may be shorter); the current line is finished first, and the last chunk becomes the current line, so later words may follow it.
- `width` must be at least 1, else `ValueError`.
