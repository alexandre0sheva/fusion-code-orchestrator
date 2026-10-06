from datetime import datetime, timedelta


def expires_at(issued_at, ttl_seconds):
    """When a token issued at ``issued_at`` stops being valid."""
    return issued_at + timedelta(seconds=ttl_seconds)


def is_expired(issued_at, ttl_seconds, now=None):
    """Whether the token has expired."""
    now = now or datetime.now()
    return now > expires_at(issued_at, ttl_seconds)
