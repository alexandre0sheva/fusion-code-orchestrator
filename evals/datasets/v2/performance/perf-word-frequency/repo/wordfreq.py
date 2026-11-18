import re


def top_words(text, k):
    """The ``k`` most frequent words as (word, count), ties in alphabetical order."""
    words = re.findall(r"[a-z']+", text.lower())
    counts = {}
    for word in set(words):
        counts[word] = words.count(word)
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return ranked[:k]
