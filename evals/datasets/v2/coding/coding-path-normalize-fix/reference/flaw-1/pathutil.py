def normalize(path):
    """The normal form of a POSIX-style path."""
    absolute = path.startswith("/")
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    joined = "/".join(parts)
    if absolute:
        return "/" + joined
    return joined or "."
