class TokenBucket:
    """A token bucket rate limiter driven by an injected clock."""

    def __init__(self, rate: float, capacity: float, clock) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._tokens = float(capacity)
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens += (now - self._last) * self.rate
        self._last = now

    def _check(self, cost: float) -> None:
        if cost <= 0 or cost > self.capacity:
            raise ValueError("bad cost")

    def allow(self, cost: float = 1) -> bool:
        self._check(cost)
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    def wait_time(self, cost: float = 1) -> float:
        self._check(cost)
        self._refill()
        return max((cost - self._tokens) / self.rate, 0.0)

    def tokens(self) -> float:
        self._refill()
        return self._tokens
