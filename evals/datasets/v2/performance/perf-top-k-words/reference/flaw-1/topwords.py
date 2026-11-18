def top_k_words(words, k):
    """The k most frequent words, most frequent first, ties in alphabetical order."""
    counts = {word: words.count(word) for word in set(words)}
    ranked = sorted(counts, key=lambda word: (-counts[word], word))
    return ranked[:max(k, 0)]
