import heapq
from collections import Counter


def top_k_words(words, k):
    """The k most frequent words, most frequent first, ties in alphabetical order."""
    if k <= 0:
        return []
    counts = Counter(words)
    best = heapq.nsmallest(k, counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in best]
