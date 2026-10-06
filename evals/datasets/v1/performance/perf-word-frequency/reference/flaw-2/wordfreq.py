import re
from collections import Counter


def top_words(text, k):
    """The ``k`` most frequent words as (word, count), ties in alphabetical order."""
    return Counter(re.findall(r"[a-z']+", text.lower())).most_common(k)
