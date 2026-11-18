def normalize(path):
    """The normal form of a POSIX-style path."""
    absolute = path.startswith("/")
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not absolute:
                parts.append("..")
        else:
            parts.append(part)
    joined = "/".join(parts)
    return "/" + joined if absolute else joined
