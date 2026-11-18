def diffstat(diff_text):
    """Count the files, added lines and removed lines of a unified diff."""
    added = removed = 0
    files = set()
    for line in diff_text.splitlines():
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
        if line.startswith("+++ "):
            files.add(line[4:])
    return {"files": len(files), "added": added, "removed": removed}
