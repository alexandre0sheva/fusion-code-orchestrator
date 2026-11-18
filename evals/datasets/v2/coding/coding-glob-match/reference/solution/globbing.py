import re


def _translate(pattern: str) -> str:
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 >= n:
                raise ValueError("pattern ends with a lone backslash")
            out.append(re.escape(pattern[i + 1]))
            i += 2
        elif c == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            negate = j < n and pattern[j] == "!"
            if negate:
                j += 1
            start = j
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                raise ValueError("unterminated character class")
            body = pattern[start:j]
            if not body:
                raise ValueError("empty character class")
            body = body.replace("\\", "\\\\").replace("^", "\\^").replace("[", "\\[")
            out.append(("[^/" if negate else "(?!/)[") + body + "]")
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def glob_match(pattern: str, name: str) -> bool:
    """Whether ``name`` matches the glob ``pattern``."""
    try:
        regex = re.compile(_translate(pattern), re.DOTALL)
    except re.error as exc:
        raise ValueError(f"bad pattern {pattern!r}: {exc}") from exc
    return regex.fullmatch(name) is not None
