def parse_query(qs):
    """Parse a query string into {key: [values]}."""
    result = {}
    for pair in qs.split("&"):
        key, value = pair.split("=")
        result[key] = value
    return result
