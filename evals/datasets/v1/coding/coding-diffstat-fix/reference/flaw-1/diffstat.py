def diffstat(diff_text):
    added = removed = 0
    files = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            name = line[4:].split("\t")[0]
            if name != "/dev/null":
                files.add(name[2:] if name.startswith("b/") else name)
        elif line.startswith("--- "):
            continue
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"files": len(files), "added": added, "removed": removed}
