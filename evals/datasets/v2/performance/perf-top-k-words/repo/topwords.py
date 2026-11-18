def top_k_words(words, k):
    """The k most frequent words, most frequent first, ties in alphabetical order."""
    distinct = []
    for word in words:
        if word not in distinct:
            distinct.append(word)
    ranked = sorted(distinct, key=lambda word: (-words.count(word), word))
    return ranked[:max(k, 0)]
