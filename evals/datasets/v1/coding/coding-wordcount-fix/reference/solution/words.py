import re
from collections import Counter

_WORD = re.compile(r"[a-z]+(?:'[a-z]+)*")


def top_words(text, n):
    """The ``n`` most frequent words in ``text`` as (word, count) tuples."""
    if n < 0:
        raise ValueError("n must not be negative")
    counts = Counter(_WORD.findall(text.lower()))
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:n]
