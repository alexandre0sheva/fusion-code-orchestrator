import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_fields_are_labelled_and_have_autocomplete(self):
        self.assertEqual(self.page.unlabelled_controls(), [])
        self.assertEqual(self.page.first("input", type="text").get("autocomplete"), "username")
        self.assertEqual(self.page.first("input", type="password").get("autocomplete"), "current-password")

    def test_dark_theme_keeps_enough_contrast(self):
        self.assertEqual(self.page.contrast_failures(), [])

    def test_focus_is_styled_and_not_removed(self):
        self.assertTrue(self.page.has_focus_style())
        self.assertNotIn("outline: none", self.page.css.replace("outline:none", "outline: none"))

    def test_show_password_toggle(self):
        box = self.page.first("input", type="checkbox")
        self.assertIsNotNone(box)
        self.assertIn("show", self.page.label_of(box).lower())
        self.assertIn("password", self.page.js)
        self.assertIn("addEventListener", self.page.js)

    def test_heading_and_landmark(self):
        self.assertEqual([h[0] for h in self.page.headings()][:1], [1])
        self.assertTrue(self.page.find("main"))

    def test_submit_and_forgot_link(self):
        self.assertTrue(any("sign in" in self.page.name_of(b).lower() for b in self.page.find("button")))
        self.assertTrue(any("forgot" in self.page.name_of(a).lower() for a in self.page.find("a")))

    def test_fits_small_screens(self):
        self.assertTrue(self.page.is_fluid())
