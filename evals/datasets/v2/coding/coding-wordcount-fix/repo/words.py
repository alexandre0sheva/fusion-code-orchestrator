from collections import Counter


def top_words(text, n):
    """The ``n`` most frequent words in ``text`` as (word, count) tuples."""
    words = text.split()
    return Counter(words).most_common(n)
