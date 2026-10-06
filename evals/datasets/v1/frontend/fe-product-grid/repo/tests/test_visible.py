import os
import unittest


class VisibleTests(unittest.TestCase):
    def test_entry_page_exists(self):
        self.assertTrue(os.path.exists("index.html"))

    def test_entry_page_is_html(self):
        with open("index.html", encoding="utf-8") as handle:
            text = handle.read().lower()
        self.assertIn("<html", text)
        self.assertIn("<body", text)
