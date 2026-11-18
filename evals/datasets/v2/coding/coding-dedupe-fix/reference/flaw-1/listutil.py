def unique(items, key=None):
    """Return ``items`` without duplicates, keeping the first of each."""
    return list(dict.fromkeys(items))
