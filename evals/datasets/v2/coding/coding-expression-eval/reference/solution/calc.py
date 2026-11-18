import re

_TOKEN = re.compile(r"\s*(?:(\d+\.\d*|\.\d+|\d+)|(\*\*|[-+*/()]))")


def _tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None:
            raise ValueError(f"unexpected character at position {pos}: {text[pos:pos + 1]!r}")
        number, op = match.groups()
        out.append(("num", number) if number is not None else ("op", op))
        pos = match.end()
    return out


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise ValueError("unexpected end of expression")
        self.pos += 1
        return token

    def at_op(self, *ops: str) -> bool:
        token = self.peek()
        return token is not None and token[0] == "op" and token[1] in ops

    def expression(self) -> float:
        value = self.term()
        while self.at_op("+", "-"):
            op = self.take()[1]
            right = self.term()
            value = value + right if op == "+" else value - right
        return value

    def term(self) -> float:
        value = self.unary()
        while self.at_op("*", "/"):
            op = self.take()[1]
            right = self.unary()
            if op == "*":
                value *= right
            else:
                if right == 0:
                    raise ZeroDivisionError("division by zero")
                value /= right
        return value

    def unary(self) -> float:
        if self.at_op("+", "-"):
            op = self.take()[1]
            value = self.unary()
            return -value if op == "-" else value
        return self.power()

    def power(self) -> float:
        base = self.atom()
        if self.at_op("**"):
            self.take()
            return base ** self.unary()
        return base

    def atom(self) -> float:
        kind, value = self.take()
        if kind == "num":
            return float(value)
        if value == "(":
            inner = self.expression()
            if not self.at_op(")"):
                raise ValueError("missing closing parenthesis")
            self.take()
            return inner
        raise ValueError(f"unexpected {value!r}")


def evaluate(text: str) -> float:
    """Evaluate an arithmetic expression."""
    tokens = _tokens(text)
    if not tokens:
        raise ValueError("empty expression")
    parser = _Parser(tokens)
    value = parser.expression()
    if parser.peek() is not None:
        raise ValueError(f"unexpected {parser.peek()[1]!r}")
    return value
