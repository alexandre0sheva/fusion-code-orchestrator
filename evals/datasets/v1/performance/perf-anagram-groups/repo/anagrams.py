def group_anagrams(words):
    """Lists of mutually anagrammatic words, in order of first appearance."""
    groups = []
    for word in words:
        key = sorted(word)
        for group_key, members in groups:
            if group_key == key:
                members.append(word)
                break
        else:
            groups.append((key, [word]))
    return [members for _, members in groups]
