def normalize(path):
    """The normal form of a POSIX-style path."""
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            parts.pop()
        else:
            parts.append(part)
    return "/" + "/".join(parts)
