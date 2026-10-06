import re

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_PART = re.compile(r"\s*(\d+)\s*([a-z])")


def parse_duration(text: str) -> int:
    """Return the length of ``text`` (like "1h30m") in seconds."""
    s = text.strip().lower()
    if not s:
        raise ValueError("empty duration")
    if s.isdigit():
        return int(s)
    total, seen, pos = 0, set(), 0
    while pos < len(s):
        match = _PART.match(s, pos)
        if match is None:
            raise ValueError(f"cannot parse duration {text!r}")
        number, unit = int(match.group(1)), match.group(2)
        if unit not in _UNITS:
            raise ValueError(f"unknown unit {unit!r}")
        if unit in seen:
            raise ValueError(f"unit {unit!r} used twice")
        seen.add(unit)
        total += number * _UNITS[unit]
        pos = match.end()
    return total
