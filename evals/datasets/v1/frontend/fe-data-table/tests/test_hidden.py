import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_table_has_a_caption(self):
        table = self.page.first("table")
        self.assertIsNotNone(table)
        self.assertTrue([e for e in table.children if e.tag == "caption"][0].text)

    def test_column_and_row_headers_are_scoped(self):
        cols = [e for e in self.page.find("th") if e.get("scope") == "col"]
        rows = [e for e in self.page.find("th") if e.get("scope") == "row"]
        self.assertEqual(len(cols), 3)
        self.assertGreaterEqual(len(rows), 5)

    def test_each_column_has_a_named_sort_button(self):
        buttons = [b for b in self.page.find("button") if any(p.tag == "th" for p in b.ancestors())]
        self.assertEqual(len(buttons), 3)
        self.assertTrue(all(self.page.name_of(b) for b in buttons))

    def test_exactly_one_column_reports_its_sort(self):
        sorted_cols = [th for th in self.page.find("th") if th.has("aria-sort")]
        self.assertEqual(len(sorted_cols), 1)
        self.assertIn(sorted_cols[0].get("aria-sort"), {"ascending", "descending"})

    def test_script_sorts_and_updates_aria_sort(self):
        self.assertIn("aria-sort", self.page.js)
        self.assertIn("sort", self.page.js)

    def test_table_scrolls_inside_a_focusable_wrapper(self):
        wrappers = [e for e in self.page.find("div") if e.get("tabindex") == "0"]
        self.assertTrue(wrappers)
        self.assertTrue(any(d.get("overflow-x") == "auto" for _, d, _ in self.page.rules()))

    def test_five_data_rows_and_a_fluid_page(self):
        body = [e for e in self.page.find("tr") if any(p.tag == "tbody" for p in e.ancestors())]
        self.assertGreaterEqual(len(body), 5)
        self.assertTrue(self.page.is_fluid())
