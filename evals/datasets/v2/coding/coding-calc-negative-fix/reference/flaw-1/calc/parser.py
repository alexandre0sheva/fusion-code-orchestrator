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
        left = term()
        if peek() in ("+", "-"):
            op = take()
            right = expr()
            return left + right if op == "+" else left - right
        return left

    def term():
        left = unary()
        if peek() in ("*", "/"):
            op = take()
            right = term()
            if op == "*":
                return left * right
            if right == 0:
                raise ZeroDivisionError("division by zero")
            return left / right
        return left

    def unary():
        if peek() in ("+", "-"):
            op = take()
            value = unary()
            return -value if op == "-" else value
        return atom()

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
