import re


def wrap(text: str, width: int) -> list[str]:
    """Wrap ``text`` to lines of at most ``width`` characters."""
    if width < 1:
        raise ValueError("width must be at least 1")
    paragraphs = [p.split() for p in re.split(r"\n[ \t\r\f\v]*\n", text.strip())]
    out: list[str] = []
    for words in (p for p in paragraphs if p):
        if out:
            out.append("")
        line = ""
        for word in words:
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
