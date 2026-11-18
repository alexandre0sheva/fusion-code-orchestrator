import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_navigation_landmark_is_named(self):
        nav = self.page.first("nav")
        self.assertIsNotNone(nav)
        self.assertTrue(nav.get("aria-label") or nav.get("aria-labelledby"))

    def test_toggle_button_controls_the_menu(self):
        button = self.page.first("button", **{"aria-controls": True})
        self.assertIsNotNone(button)
        self.assertTrue(self.page.name_of(button))
        self.assertIsNotNone(self.page.by_id(button.get("aria-controls")))

    def test_toggle_starts_collapsed(self):
        button = self.page.first("button", **{"aria-controls": True})
        self.assertEqual(button.get("aria-expanded"), "false")

    def test_five_named_links(self):
        links = [a for a in self.page.find("a") if any(p.tag == "nav" for p in a.ancestors())]
        self.assertGreaterEqual(len(links), 5)
        self.assertTrue(all(self.page.name_of(a) for a in links))

    def test_a_script_keeps_aria_expanded_in_step(self):
        self.assertIn("aria-expanded", self.page.js)
        self.assertIn("addEventListener", self.page.js)

    def test_collapses_with_a_media_query(self):
        self.assertTrue(any("max-width" in q for q in self.page.media_queries()))
        self.assertTrue(self.page.has_viewport_meta())

    def test_keyboard_focus_is_visible(self):
        self.assertTrue(self.page.has_focus_style())
