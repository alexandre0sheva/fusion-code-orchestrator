import re


def glob_match(pattern: str, name: str) -> bool:
    """Whether ``name`` matches the glob ``pattern``."""
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.fullmatch("".join(out), name, re.DOTALL) is not None
