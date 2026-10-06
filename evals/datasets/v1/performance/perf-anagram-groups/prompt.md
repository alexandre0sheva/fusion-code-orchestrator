`group_anagrams(words)` groups words that are anagrams of each other (same letters, same case). Groups are returned in the order their first word appears, and the words of a group keep the order they had in the input.

```python
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
```

Make `group_anagrams` fast for large inputs (10^5 words) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
