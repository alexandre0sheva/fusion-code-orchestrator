import re

_TOKEN = re.compile(r"\s*(-?\d+(?:\.\d+)?|[-+*/()])")


def tokenize(text):
    tokens = []
    pos = 0
    while pos < len(text.rstrip()):
        match = _TOKEN.match(text, pos)
        if match is None:
            raise ValueError(f"unexpected character at {pos}")
        tokens.append(match.group(1))
        pos = match.end()
    return tokens
