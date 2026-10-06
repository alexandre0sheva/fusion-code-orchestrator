import re

_VERSION = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([^+]*))?(?:\+(.*))?$"
)


def _parse(text: str):
    match = _VERSION.match(text)
    if match is None:
        raise ValueError(f"malformed version: {text!r}")
    core = tuple(int(match.group(i)) for i in (1, 2, 3))
    pre = match.group(4)
    if pre is None:
        return core, None
    identifiers = pre.split(".")
    for ident in identifiers:
        if not ident or not re.fullmatch(r"[0-9A-Za-z-]+", ident):
            raise ValueError(f"malformed prerelease in {text!r}")
        if ident.isdigit() and len(ident) > 1 and ident.startswith("0"):
            raise ValueError(f"leading zero in prerelease of {text!r}")
    return core, identifiers


def _cmp(x, y) -> int:
    return (x > y) - (x < y)


def compare_versions(a: str, b: str) -> int:
    """Return -1, 0 or 1 by Semantic Versioning 2.0.0 precedence."""
    core_a, pre_a = _parse(a)
    core_b, pre_b = _parse(b)
    if core_a != core_b:
        return _cmp(core_a, core_b)
    if pre_a is None and pre_b is None:
        return 0
    if pre_a is None:
        return 1
    if pre_b is None:
        return -1
    for x, y in zip(pre_a, pre_b):
        if x == y:
            continue
        x_num, y_num = x.isdigit(), y.isdigit()
        if x_num and y_num:
            return _cmp(int(x), int(y))
        if x_num != y_num:
            return -1 if x_num else 1
        return _cmp(x, y)
    return _cmp(len(pre_a), len(pre_b))
