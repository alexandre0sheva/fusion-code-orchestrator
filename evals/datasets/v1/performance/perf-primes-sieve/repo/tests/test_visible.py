import unittest

from primes import primes_up_to


class VisibleTests(unittest.TestCase):
    def test_primes_up_to_ten(self):
        self.assertEqual(primes_up_to(10), [2, 3, 5, 7])

    def test_below_two(self):
        self.assertEqual(primes_up_to(1), [])
