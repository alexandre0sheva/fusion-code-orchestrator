def longest_unique_run(text):
    """The longest substring without a repeated character (the first one on ties)."""
    best = ""
    for start in range(len(text)):
        end = start
        while end < len(text) and text[end] not in text[start:end]:
            end += 1
        if end - start > len(best):
            best = text[start:end]
    return best
