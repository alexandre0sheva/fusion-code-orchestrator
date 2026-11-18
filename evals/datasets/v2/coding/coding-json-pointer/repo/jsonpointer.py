class PointerError(LookupError):
    """The pointer does not lead to anything in the document."""


def pointer_get(doc, pointer: str):
    """Return the value ``pointer`` points to."""
    raise NotImplementedError


def pointer_set(doc, pointer: str, value):
    """Set the value ``pointer`` points to, in place, and return ``doc``."""
    raise NotImplementedError
