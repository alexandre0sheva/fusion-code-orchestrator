import unittest

from pagination import paginate


class VisibleTests(unittest.TestCase):
    def test_first_page(self):
        result = paginate(list(range(10)), 1, 5)
        self.assertEqual(result["items"], [0, 1, 2, 3, 4])

    def test_metadata(self):
        result = paginate(list(range(10)), 1, 5)
        self.assertEqual((result["pages"], result["total"]), (2, 10))
