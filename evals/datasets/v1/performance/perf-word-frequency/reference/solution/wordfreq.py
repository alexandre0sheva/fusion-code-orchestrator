import re
from collections import Counter


def top_words(text, k):
    """The ``k`` most frequent words as (word, count), ties in alphabetical order."""
    counts = Counter(re.findall(r"[a-z']+", text.lower()))
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return ranked[:k]
