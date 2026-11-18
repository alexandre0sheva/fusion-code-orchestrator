def primes_up_to(n):
    """All primes p with 2 <= p <= n, ascending."""
    found = []
    for candidate in range(2, n + 1):
        for prime in found:
            if prime * prime > candidate:
                found.append(candidate)
                break
            if candidate % prime == 0:
                break
        else:
            found.append(candidate)
    return found
