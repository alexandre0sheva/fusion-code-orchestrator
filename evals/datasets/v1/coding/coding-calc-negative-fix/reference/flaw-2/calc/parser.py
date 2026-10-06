from calc.tokenizer import tokenize


def evaluate(text):
    tokens = tokenize(text)
    if not tokens:
        raise ValueError("empty expression")
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def take():
        nonlocal pos
        token = peek()
        if token is None:
            raise ValueError("unexpected end")
        pos += 1
        return token

    def expr():
        value = term()
        while peek() in ("+", "-"):
            op = take()
            right = term()
            value = value + right if op == "+" else value - right
        return value

    def term():
        value = atom()
        while peek() in ("*", "/"):
            op = take()
            right = atom()
            if op == "*":
                value = value * right
            else:
                if right == 0:
                    raise ZeroDivisionError("division by zero")
                value = value / right
        return value

    def atom():
        token = take()
        if token == "(":
            value = expr()
            if take() != ")":
                raise ValueError("expected )")
            return value
        try:
            return float(token)
        except ValueError:
            raise ValueError(f"unexpected token {token!r}") from None

    value = expr()
    if pos != len(tokens):
        raise ValueError("unexpected trailing input")
    return value
