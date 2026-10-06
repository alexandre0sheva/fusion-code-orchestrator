def merge(base, override):
    """Merge ``override`` into ``base``."""
    result = base
    for key, value in override.items():
        result[key] = value
    return result
