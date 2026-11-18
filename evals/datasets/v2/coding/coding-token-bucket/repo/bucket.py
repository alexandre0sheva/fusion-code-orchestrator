class TokenBucket:
    """A token bucket rate limiter driven by an injected clock."""

    def __init__(self, rate: float, capacity: float, clock) -> None:
        raise NotImplementedError

    def allow(self, cost: float = 1) -> bool:
        raise NotImplementedError

    def wait_time(self, cost: float = 1) -> float:
        raise NotImplementedError

    def tokens(self) -> float:
        raise NotImplementedError
