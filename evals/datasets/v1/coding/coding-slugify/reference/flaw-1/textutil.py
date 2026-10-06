import re


def slugify(text: str, max_length: int | None = None) -> str:
    """Return a URL slug for ``text``."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if max_length is not None:
        slug = slug[:max_length].rstrip("-")
    return slug
