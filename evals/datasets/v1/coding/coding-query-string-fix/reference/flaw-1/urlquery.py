def parse_query(qs):
    """Parse a query string into {key: [values]}."""
    if qs.startswith("?"):
        qs = qs[1:]
    result = {}
    for pair in qs.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        result.setdefault(key, []).append(value)
    return result
