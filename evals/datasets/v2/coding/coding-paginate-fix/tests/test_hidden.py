import unittest

from pagination import paginate


class PaginateTests(unittest.TestCase):
    def test_pages_are_one_based(self):
        data = list(range(1, 12))
        self.assertEqual(paginate(data, 1, 5)["items"], [1, 2, 3, 4, 5])
        self.assertEqual(paginate(data, 2, 5)["items"], [6, 7, 8, 9, 10])

    def test_last_partial_page(self):
        result = paginate(list(range(1, 12)), 3, 5)
        self.assertEqual(result["items"], [11])

    def test_page_count_rounds_up(self):
        self.assertEqual(paginate(list(range(11)), 1, 5)["pages"], 3)
        self.assertEqual(paginate(list(range(10)), 1, 5)["pages"], 2)
        self.assertEqual(paginate(list(range(1)), 1, 5)["pages"], 1)

    def test_no_items(self):
        result = paginate([], 1, 5)
        self.assertEqual(result, {"items": [], "page": 1, "pages": 0, "total": 0})

    def test_page_past_the_end_is_empty(self):
        result = paginate([1, 2, 3], 5, 2)
        self.assertEqual(result["items"], [])
        self.assertEqual((result["pages"], result["total"], result["page"]), (2, 3, 5))

    def test_validation(self):
        for page, per_page in ((0, 5), (-1, 5), (1, 0), (1, -2)):
            with self.assertRaises(ValueError):
                paginate([1, 2, 3], page, per_page)

    def test_input_is_not_modified_and_result_is_a_copy(self):
        data = [1, 2, 3]
        result = paginate(data, 1, 5)
        result["items"].append(4)
        self.assertEqual(data, [1, 2, 3])
