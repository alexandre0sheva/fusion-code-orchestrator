def render_newest_first(lines):
    """One string, newest line first, each as "<position>\t<text>"."""
    entries = [f"{position}\t{line}" for position, line in enumerate(lines, 1)]
    return "\n".join(entries[::-1]) if entries[0] else ""
