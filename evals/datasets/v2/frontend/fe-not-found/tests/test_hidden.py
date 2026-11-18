import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_heading_says_the_page_was_not_found(self):
        h1 = [text for level, text in self.page.headings() if level == 1]
        self.assertEqual(len(h1), 1)
        self.assertIn("not found", h1[0].lower())

    def test_search_form_sends_a_get_to_search(self):
        form = self.page.first("form")
        self.assertIsNotNone(form)
        self.assertEqual(form.get("action"), "/search")
        self.assertEqual(form.get("method").lower(), "get")

    def test_search_field_is_labelled(self):
        field = self.page.first("input", type="search")
        self.assertIsNotNone(field)
        self.assertTrue(self.page.label_of(field))
        self.assertEqual(self.page.unlabelled_controls(), [])

    def test_search_has_a_submit_button(self):
        buttons = self.page.find("button") + self.page.find("input", type="submit")
        self.assertTrue(any(self.page.name_of(b) for b in buttons))

    def test_link_back_home(self):
        self.assertTrue(self.page.find("a", href="/"))

    def test_popular_pages_in_a_navigation_landmark(self):
        navs = self.page.find("nav")
        self.assertTrue(navs)
        links = [e for nav in navs for e in nav.walk() if e.tag == "a"]
        self.assertGreaterEqual(len(links), 3)

    def test_main_landmark_and_focus_style(self):
        self.assertTrue(self.page.find("main"))
        self.assertTrue(self.page.has_focus_style())

    def test_fluid_layout(self):
        self.assertTrue(self.page.is_fluid())
