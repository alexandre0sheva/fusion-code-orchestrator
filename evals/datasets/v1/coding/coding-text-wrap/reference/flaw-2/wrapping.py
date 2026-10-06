def wrap(text: str, width: int) -> list[str]:
    """Wrap ``text`` to lines of at most ``width`` characters."""
    if width < 1:
        raise ValueError("width must be at least 1")
    out: list[str] = []
    line = ""
    for word in text.split():
        while len(word) > width:
            if line:
                out.append(line)
                line = ""
            out.append(word[:width])
            word = word[width:]
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            out.append(line)
            line = word
    if line:
        out.append(line)
    return out
