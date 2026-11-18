import re

_TOKEN = re.compile(r"\s*(?:(\d+\.\d*|\.\d+|\d+)|(\*\*|[-+*/()]))")


def evaluate(text):
    tokens = []
    pos = 0
    while pos < len(text.rstrip()):
        match = _TOKEN.match(text, pos)
        if match is None:
            break
        number, op = match.groups()
        tokens.append(float(number) if number is not None else op)
        pos = match.end()
    if not tokens:
        raise ValueError("empty expression")
    return _expr(tokens)


def _expr(tokens):
    value = _term(tokens)
    while tokens and tokens[0] in ("+", "-"):
        op = tokens.pop(0)
        right = _term(tokens)
        value = value + right if op == "+" else value - right
    return value


def _term(tokens):
    value = _power(tokens)
    while tokens and tokens[0] in ("*", "/"):
        op = tokens.pop(0)
        right = _power(tokens)
        value = value * right if op == "*" else value / right
    return value


def _power(tokens):
    base = _unary(tokens)
    if tokens and tokens[0] == "**":
        tokens.pop(0)
        return base ** _power(tokens)
    return base


def _unary(tokens):
    if not tokens:
        raise ValueError("missing operand")
    head = tokens.pop(0)
    if head == "-":
        return -_unary(tokens)
    if head == "+":
        return _unary(tokens)
    if head == "(":
        value = _expr(tokens)
        if tokens and tokens[0] == ")":
            tokens.pop(0)
        return value
    if isinstance(head, float):
        return head
    raise ValueError("unexpected token")
