def render_newest_first(lines):
    """One string, newest line first, each as "<position>\t<text>"."""
    text = ""
    for position, line in enumerate(lines, 1):
        entry = f"{position}\t{line}"
        text = entry + ("\n" + text if text else "")
    return text
