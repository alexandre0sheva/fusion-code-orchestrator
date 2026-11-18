`tokens.py` decides when an access token expires. Two bugs are reported: `is_expired(issued_at, ttl)` raises `TypeError: can't compare offset-naive and offset-aware datetimes` when `now` is left out, and a token whose lifetime has run out exactly (now equals issue time plus ttl) is still reported as valid.

Fix it so that it behaves as documented:

- `expires_at(issued_at, ttl_seconds)` returns a timezone-aware datetime in UTC. A naive `issued_at` is taken to be UTC; an aware one in any timezone is converted. A negative `ttl_seconds` raises `ValueError`.
- `is_expired(issued_at, ttl_seconds, now=None)` is `True` from the moment `expires_at` is reached (`now >= expires_at`). `now` defaults to the current time in UTC; a naive `now` is also taken to be UTC and an aware one may be in any timezone.
