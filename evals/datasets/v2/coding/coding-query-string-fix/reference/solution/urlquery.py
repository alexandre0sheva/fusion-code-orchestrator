import re

_ESCAPES = re.compile(r"(?:%[0-9A-Fa-f]{2})+")


def _decode(text):
    text = text.replace("+", " ")

    def replace(match):
        raw = bytes.fromhex(match.group(0).replace("%", ""))
        return raw.decode("utf-8", errors="replace")

    return _ESCAPES.sub(replace, text)


def parse_query(qs):
    """Parse a query string into {key: [values]}."""
    if qs.startswith("?"):
        qs = qs[1:]
    result = {}
    for pair in qs.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        result.setdefault(_decode(key), []).append(_decode(value))
    return result
