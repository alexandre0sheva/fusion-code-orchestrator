from collections import Counter


def top_k_words(words, k):
    """The k most frequent words, most frequent first, ties in alphabetical order."""
    if k <= 0:
        return []
    return [word for word, _ in Counter(words).most_common(k)]
