import re

_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([^+]*))?(?:\+(.*))?$")


def _parse(text: str):
    match = _VERSION.match(text)
    if match is None:
        raise ValueError(f"malformed version: {text!r}")
    core = tuple(int(match.group(i)) for i in (1, 2, 3))
    return core, match.group(4), match.group(5) or ""


def _cmp(x, y) -> int:
    return (x > y) - (x < y)


def _ident(x: str):
    return (0, int(x), "") if x.isdigit() else (1, 0, x)


def compare_versions(a: str, b: str) -> int:
    """Return -1, 0 or 1 by Semantic Versioning 2.0.0 precedence."""
    core_a, pre_a, build_a = _parse(a)
    core_b, pre_b, build_b = _parse(b)
    if core_a != core_b:
        return _cmp(core_a, core_b)
    if pre_a != pre_b:
        if pre_a is None:
            return 1
        if pre_b is None:
            return -1
        return _cmp([_ident(x) for x in pre_a.split(".")], [_ident(x) for x in pre_b.split(".")])
    return _cmp(build_a, build_b)
