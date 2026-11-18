def longest_unique_run(text):
    """The longest substring without a repeated character (the first one on ties)."""
    last = {}
    left = 0
    best_start, best_len = 0, 0
    for right, char in enumerate(text):
        if last.get(char, -1) >= left:
            left = last[char] + 1
        last[char] = right
        if right - left + 1 > best_len:
            best_start, best_len = left, right - left + 1
    return text[best_start:best_start + best_len]
