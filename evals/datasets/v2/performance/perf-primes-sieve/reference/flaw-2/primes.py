def primes_up_to(n):
    """All primes p with 2 <= p <= n, ascending."""
    if n < 3:
        return []
    sieve = bytearray([1]) * n
    sieve[0] = sieve[1] = 0
    for p in range(2, int(n**0.5) + 1):
        if sieve[p]:
            sieve[p * p :: p] = bytes(len(range(p * p, n, p)))
    return [i for i, flag in enumerate(sieve) if flag]
