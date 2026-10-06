Implement `slugify(text, max_length=None)` in `textutil.py`.

- Lower-case the text and fold accented Latin letters to plain ASCII (`é` becomes `e`).
- Every run of characters that are not ASCII letters or digits becomes a single `-`.
- The result has no leading or trailing `-`. If nothing is left, return `""`.
- If `max_length` is given, cut the slug to at most that many characters; the cut slug must not end in `-`.
