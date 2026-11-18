import fnmatch


def glob_match(pattern: str, name: str) -> bool:
    """Whether ``name`` matches the glob ``pattern``."""
    return fnmatch.fnmatchcase(name, pattern)
