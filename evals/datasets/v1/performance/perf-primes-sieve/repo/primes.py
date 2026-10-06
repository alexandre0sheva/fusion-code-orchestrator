def primes_up_to(n):
    """All primes p with 2 <= p <= n, ascending."""
    return [
        candidate
        for candidate in range(2, n + 1)
        if all(candidate % divisor for divisor in range(2, int(candidate**0.5) + 1))
    ]
