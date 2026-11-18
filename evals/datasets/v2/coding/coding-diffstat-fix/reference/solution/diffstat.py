import re

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _name(raw):
    raw = raw.split("\t")[0].strip()
    if raw == "/dev/null":
        return None
    return raw[2:] if raw.startswith(("a/", "b/")) else raw


def diffstat(diff_text):
    """Count the files, added lines and removed lines of a unified diff."""
    lines = diff_text.splitlines()
    files = set()
    added = removed = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        hunk = _HUNK.match(line)
        if hunk:
            old_left = int(hunk.group(1)) if hunk.group(1) is not None else 1
            new_left = int(hunk.group(2)) if hunk.group(2) is not None else 1
            i += 1
            while i < len(lines) and (old_left > 0 or new_left > 0):
                body = lines[i]
                if body.startswith("\\"):
                    i += 1
                    continue
                if body.startswith("+"):
                    added += 1
                    new_left -= 1
                elif body.startswith("-"):
                    removed += 1
                    old_left -= 1
                else:
                    old_left -= 1
                    new_left -= 1
                i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            name = _name(lines[i + 1][4:]) or _name(line[4:])
            if name:
                files.add(name)
            i += 2
            continue
        i += 1
    return {"files": len(files), "added": added, "removed": removed}
