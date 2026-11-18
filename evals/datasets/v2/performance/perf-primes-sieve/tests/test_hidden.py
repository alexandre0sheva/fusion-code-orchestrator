import unittest

from primes import primes_up_to


class HiddenTests(unittest.TestCase):
    def test_includes_n_when_prime(self):
        self.assertEqual(primes_up_to(13), [2, 3, 5, 7, 11, 13])

    def test_two(self):
        self.assertEqual(primes_up_to(2), [2])

    def test_zero_and_negative(self):
        self.assertEqual(primes_up_to(0), [])
        self.assertEqual(primes_up_to(-5), [])

    def test_squares_of_primes_are_not_prime(self):
        self.assertNotIn(49, primes_up_to(60))
        self.assertNotIn(25, primes_up_to(60))

    def test_count_below_a_hundred(self):
        self.assertEqual(len(primes_up_to(100)), 25)

    def test_last_prime_below_a_thousand(self):
        self.assertEqual(primes_up_to(1000)[-1], 997)
