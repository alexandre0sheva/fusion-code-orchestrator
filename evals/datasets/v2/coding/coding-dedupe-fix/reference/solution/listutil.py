def unique(items, key=None):
    """Return ``items`` without duplicates, keeping the first of each."""
    result = []
    seen_hashable = set()
    seen_other = []
    for item in items:
        marker = item if key is None else key(item)
        try:
            if marker in seen_hashable:
                continue
            seen_hashable.add(marker)
        except TypeError:
            if any(marker == earlier for earlier in seen_other):
                continue
            seen_other.append(marker)
        result.append(item)
    return result
