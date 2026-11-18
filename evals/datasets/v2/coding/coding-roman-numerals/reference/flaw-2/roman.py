import re

_ADDITIVE = [(1000, "M"), (500, "D"), (100, "C"), (50, "L"), (10, "X"), (5, "V"), (1, "I")]
_CANONICAL = re.compile(r"^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
_TABLE = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def to_roman(n: int) -> str:
    """Return the Roman numeral for ``n`` (1..3999)."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError("n must be an int")
    if not 1 <= n <= 3999:
        raise ValueError("n must be between 1 and 3999")
    out = []
    for value, symbol in _ADDITIVE:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


def from_roman(text: str) -> int:
    """Return the integer a canonical Roman numeral stands for."""
    s = text.upper()
    if not s or not _CANONICAL.match(s):
        raise ValueError("not a canonical Roman numeral")
    total = 0
    for value, symbol in _TABLE:
        while s.startswith(symbol):
            total += value
            s = s[len(symbol):]
    return total
