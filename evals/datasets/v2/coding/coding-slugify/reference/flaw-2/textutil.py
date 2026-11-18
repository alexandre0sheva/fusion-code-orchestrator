import re
import unicodedata


def slugify(text: str, max_length: int | None = None) -> str:
    """Return a URL slug for ``text``."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    if max_length is not None:
        slug = slug[:max_length]
    return slug
