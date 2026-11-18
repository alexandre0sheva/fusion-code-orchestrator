import re

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> int:
    """Return the length of ``text`` (like "1h30m") in seconds."""
    parts = re.findall(r"(\d+)\s*([dhms])", text.lower())
    if not parts:
        raise ValueError("cannot parse duration")
    return sum(int(n) * _UNITS[u] for n, u in parts)
