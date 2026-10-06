def render_newest_first(lines):
    """One string, newest line first, each as "<position>\t<text>"."""
    entries = []
    for position, line in enumerate(lines, 1):
        entries.insert(0, f"{position}\t{line}")
    return "\n".join(entries)
