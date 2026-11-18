_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_TABLE = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def to_roman(n: int) -> str:
    """Return the Roman numeral for ``n`` (1..3999)."""
    if not isinstance(n, int):
        raise TypeError("n must be an int")
    if not 1 <= n <= 3999:
        raise ValueError("n must be between 1 and 3999")
    out = []
    for value, symbol in _TABLE:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


def from_roman(text: str) -> int:
    """Return the integer a canonical Roman numeral stands for."""
    s = text.upper()
    if not s or any(c not in _VALUES for c in s):
        raise ValueError("not a Roman numeral")
    total = 0
    for i, c in enumerate(s):
        if i + 1 < len(s) and _VALUES[c] < _VALUES[s[i + 1]]:
            total -= _VALUES[c]
        else:
            total += _VALUES[c]
    return total
