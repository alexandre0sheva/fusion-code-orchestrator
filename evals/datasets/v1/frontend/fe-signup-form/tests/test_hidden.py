import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_every_control_has_a_label(self):
        self.assertEqual(self.page.unlabelled_controls(), [])

    def test_email_field(self):
        field = self.page.first("input", type="email")
        self.assertIsNotNone(field)
        self.assertTrue(field.has("required"))
        self.assertEqual(field.get("autocomplete"), "email")

    def test_password_field_rules(self):
        field = self.page.first("input", type="password")
        self.assertIsNotNone(field)
        self.assertTrue(field.has("required"))
        self.assertGreaterEqual(int(field.get("minlength", "0") or 0), 8)
        self.assertEqual(field.get("autocomplete"), "new-password")

    def test_hints_are_attached_to_their_fields(self):
        for kind in ("email", "password"):
            field = self.page.first("input", type=kind)
            hint = self.page.by_id(field.get("aria-describedby").split()[0]) if field.get("aria-describedby") else None
            self.assertIsNotNone(hint, kind)
            self.assertTrue(hint.text)

    def test_terms_checkbox_is_required_and_named(self):
        box = self.page.first("input", type="checkbox")
        self.assertIsNotNone(box)
        self.assertTrue(box.has("required"))
        self.assertIn("terms", self.page.label_of(box).lower())

    def test_submit_button(self):
        buttons = [b for b in self.page.find("button") if b.get("type", "submit") == "submit"]
        self.assertTrue(buttons)
        self.assertIn("account", self.page.name_of(buttons[0]).lower())

    def test_fits_small_screens_and_has_one_h1(self):
        self.assertTrue(self.page.is_fluid())
        self.assertEqual(len([h for h in self.page.headings() if h[0] == 1]), 1)
