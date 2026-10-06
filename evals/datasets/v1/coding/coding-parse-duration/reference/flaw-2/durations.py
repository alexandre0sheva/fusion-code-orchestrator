import re

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_FULL = re.compile(r"^(?:\s*\d+[dhms])+\s*$")


def parse_duration(text: str) -> int:
    """Return the length of ``text`` (like "1h30m") in seconds."""
    if not _FULL.match(text):
        raise ValueError(f"cannot parse duration {text!r}")
    units = re.findall(r"\d+([dhms])", text)
    if len(set(units)) != len(units):
        raise ValueError("unit used twice")
    return sum(int(n) * _UNITS[u] for n, u in re.findall(r"(\d+)([dhms])", text))
