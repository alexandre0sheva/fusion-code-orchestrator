def unique(items, key=None):
    """Return ``items`` without duplicates, keeping the first of each."""
    result = []
    for item in items:
        if not any(item == earlier for earlier in result):
            result.append(item)
    return result
