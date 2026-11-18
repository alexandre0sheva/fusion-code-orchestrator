def primes_up_to(n):
    """All primes p with 2 <= p <= n, ascending."""
    if n < 2:
        return []
    sieve = bytearray([1]) * (n + 1)
    sieve[0] = sieve[1] = 0
    for p in range(2, int(n**0.5) + 1):
        if sieve[p]:
            sieve[p * p :: p] = bytes(len(range(p * p, n + 1, p)))
    return [i for i, flag in enumerate(sieve) if flag]
