from datetime import datetime, timedelta, timezone


def _utc(moment):
    """``moment`` as an aware UTC datetime (a naive one is taken to be UTC)."""
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def expires_at(issued_at, ttl_seconds):
    """When a token issued at ``issued_at`` stops being valid."""
    if ttl_seconds < 0:
        raise ValueError("ttl_seconds must not be negative")
    return _utc(issued_at) + timedelta(seconds=ttl_seconds)


def is_expired(issued_at, ttl_seconds, now=None):
    """Whether the token has expired."""
    deadline = expires_at(issued_at, ttl_seconds)
    current = datetime.now(timezone.utc) if now is None else _utc(now)
    return current >= deadline
