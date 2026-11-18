def group_anagrams(words):
    """Lists of mutually anagrammatic words, in order of first appearance."""
    groups = {}
    for word in words:
        groups.setdefault("".join(sorted(word)), []).append(word)
    return [sorted(members) for members in groups.values()]
