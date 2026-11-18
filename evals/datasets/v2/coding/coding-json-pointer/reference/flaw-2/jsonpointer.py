class PointerError(LookupError):
    """The pointer does not lead to anything in the document."""


def _tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("a JSON pointer must start with '/'")
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer[1:].split("/")]


def _step(node, token: str):
    if isinstance(node, dict):
        if token not in node:
            raise PointerError(f"no key {token!r}")
        return node[token]
    if isinstance(node, list):
        try:
            return node[int(token)]
        except (ValueError, IndexError) as exc:
            raise PointerError(f"no index {token!r}") from exc
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
        try:
            parent[int(last)] = value
        except (ValueError, IndexError) as exc:
            raise PointerError(f"no index {last!r}") from exc
    else:
        raise PointerError("cannot set inside a scalar")
    return doc
