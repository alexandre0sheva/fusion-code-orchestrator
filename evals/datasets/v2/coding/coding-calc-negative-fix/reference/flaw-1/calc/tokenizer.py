import re

_TOKEN = re.compile(r"\s*(\d+(?:\.\d+)?|\.\d+|[-+*/()])")


def tokenize(text):
    tokens = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None:
            raise ValueError(f"unexpected character at {pos}")
        tokens.append(match.group(1))
        pos = match.end()
    return tokens
