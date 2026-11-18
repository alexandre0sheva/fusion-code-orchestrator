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
    if pre is not None and any(not i for i in pre.split(".")):
        raise ValueError("empty identifier")
    return core, pre


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
    return _cmp(pre_a, pre_b)
