import unittest

from semver import compare_versions


class CompareVersionsTests(unittest.TestCase):
    def test_numeric_parts_compare_as_integers(self):
        self.assertEqual(compare_versions("1.2.3", "1.10.0"), -1)
        self.assertEqual(compare_versions("2.0.0", "1.99.99"), 1)
        self.assertEqual(compare_versions("1.0.10", "1.0.9"), 1)

    def test_equal(self):
        self.assertEqual(compare_versions("1.2.3", "1.2.3"), 0)

    def test_release_beats_its_prerelease(self):
        self.assertEqual(compare_versions("1.0.0", "1.0.0-rc.1"), 1)
        self.assertEqual(compare_versions("1.0.0-rc.1", "1.0.0"), -1)

    def test_the_specs_ordering_chain(self):
        chain = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
                 "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
        for lower, higher in zip(chain, chain[1:]):
            self.assertEqual(compare_versions(lower, higher), -1, (lower, higher))
            self.assertEqual(compare_versions(higher, lower), 1, (higher, lower))

    def test_numeric_identifier_is_lower_than_alphanumeric(self):
        self.assertEqual(compare_versions("1.0.0-1", "1.0.0-a"), -1)

    def test_build_metadata_is_ignored(self):
        self.assertEqual(compare_versions("1.0.0+a", "1.0.0+b"), 0)
        self.assertEqual(compare_versions("1.0.0-rc.1+x", "1.0.0-rc.1"), 0)

    def test_shorter_prerelease_is_lower(self):
        self.assertEqual(compare_versions("1.0.0-a", "1.0.0-a.0"), -1)

    def test_malformed_versions(self):
        for bad in ("1.2", "1.2.3.4", "01.2.3", "1.2.x", "1.0.0-", "1.0.0-a..b", "", "v1.2.3"):
            with self.assertRaises(ValueError, msg=bad):
                compare_versions(bad, "1.0.0")
            with self.assertRaises(ValueError, msg=bad):
                compare_versions("1.0.0", bad)
