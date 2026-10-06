import re

_TOKEN = re.compile(r"\s*(?:(\d+\.\d*|\.\d+|\d+)|(\*\*|[-+*/()]))")


def _tokens(text):
    out = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None:
            raise ValueError("unexpected character")
        number, op = match.groups()
        out.append(("num", number) if number is not None else ("op", op))
        pos = match.end()
    return out


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        token = self.peek()
        if token is None:
            raise ValueError("unexpected end")
        self.pos += 1
        return token

    def at_op(self, *ops):
        token = self.peek()
        return token is not None and token[0] == "op" and token[1] in ops

    def expression(self):
        value = self.term()
        while self.at_op("+", "-"):
            op = self.take()[1]
            right = self.term()
            value = value + right if op == "+" else value - right
        return value

    def term(self):
        value = self.power()
        while self.at_op("*", "/"):
            op = self.take()[1]
            right = self.power()
            if op == "*":
                value *= right
            else:
                if right == 0:
                    raise ZeroDivisionError("division by zero")
                value /= right
        return value

    def power(self):
        value = self.unary()
        while self.at_op("**"):
            self.take()
            value = value ** self.unary()
        return value

    def unary(self):
        if self.at_op("+", "-"):
            op = self.take()[1]
            value = self.unary()
            return -value if op == "-" else value
        return self.atom()

    def atom(self):
        kind, value = self.take()
        if kind == "num":
            return float(value)
        if value == "(":
            inner = self.expression()
            if not self.at_op(")"):
                raise ValueError("missing closing parenthesis")
            self.take()
            return inner
        raise ValueError("unexpected token")


def evaluate(text):
    tokens = _tokens(text)
    if not tokens:
        raise ValueError("empty expression")
    parser = _Parser(tokens)
    value = parser.expression()
    if parser.peek() is not None:
        raise ValueError("unexpected token")
    return value
