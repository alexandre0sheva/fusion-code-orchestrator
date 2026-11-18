import unittest

from semver import compare_versions


class VisibleTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(compare_versions("1.2.3", "1.10.0"), -1)

    def test_equal(self):
        self.assertEqual(compare_versions("2.0.0", "2.0.0"), 0)

    def test_prerelease_is_lower(self):
        self.assertEqual(compare_versions("1.0.0-rc.1", "1.0.0"), -1)

    def test_the_specs_ordering_chain(self):
        chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
                 "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
        for lower, higher in zip(chain, chain[1:]):
            self.assertEqual(compare_versions(lower, higher), -1, (lower, higher))
            self.assertEqual(compare_versions(higher, lower), 1, (higher, lower))
