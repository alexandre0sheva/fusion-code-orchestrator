import re


class PointerError(LookupError):
    """The pointer does not lead to anything in the document."""


_INDEX = re.compile(r"0|[1-9][0-9]*")


def _tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"a JSON pointer must start with '/': {pointer!r}")
    return [t.replace("~0", "~").replace("~1", "/") for t in pointer[1:].split("/")]


def _step(node, token: str):
    if isinstance(node, dict):
        if token not in node:
            raise PointerError(f"no key {token!r}")
        return node[token]
    if isinstance(node, list):
        if not _INDEX.fullmatch(token) or int(token) >= len(node):
            raise PointerError(f"no index {token!r}")
        return node[int(token)]
    raise PointerError("cannot step into a scalar")


def pointer_get(doc, pointer: str):
    node = doc
    for token in _tokens(pointer):
        node = _step(node, token)
    return node


def pointer_set(doc, pointer: str, value):
    tokens = _tokens(pointer)
    if not tokens:
        raise ValueError("cannot replace the whole document in place")
    parent = doc
    for token in tokens[:-1]:
        parent = _step(parent, token)
    last = tokens[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list):
        if last == "-" or (_INDEX.fullmatch(last) and int(last) == len(parent)):
            parent.append(value)
        elif _INDEX.fullmatch(last) and int(last) < len(parent):
            parent[int(last)] = value
        else:
            raise PointerError(f"no index {last!r}")
    else:
        raise PointerError("cannot set inside a scalar")
    return doc
