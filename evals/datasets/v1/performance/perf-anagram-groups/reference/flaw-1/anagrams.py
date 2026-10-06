def group_anagrams(words):
    """Lists of mutually anagrammatic words, in order of first appearance."""
    keys = []
    groups = []
    for word in words:
        key = "".join(sorted(word))
        if key in keys:
            groups[keys.index(key)].append(word)
        else:
            keys.append(key)
            groups.append([word])
    return groups
