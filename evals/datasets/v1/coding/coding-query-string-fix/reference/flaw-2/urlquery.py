from urllib.parse import unquote_plus


def parse_query(qs):
    """Parse a query string into {key: [values]}."""
    if qs.startswith("?"):
        qs = qs[1:]
    result = {}
    for pair in qs.split("&"):
        if not pair:
            continue
        parts = pair.split("=")
        key = unquote_plus(parts[0])
        value = unquote_plus(parts[1]) if len(parts) > 1 else ""
        result[key] = [value]
    return result
