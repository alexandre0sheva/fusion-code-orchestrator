import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_uses_the_dialog_element_with_a_name(self):
        dialog = self.page.first("dialog")
        self.assertIsNotNone(dialog)
        title = self.page.by_id(dialog.get("aria-labelledby"))
        self.assertIsNotNone(title)
        self.assertTrue(title.text)

    def test_opener_announces_a_dialog(self):
        opener = self.page.first("button", **{"aria-haspopup": "dialog"})
        self.assertIsNotNone(opener)
        self.assertTrue(self.page.name_of(opener))

    def test_script_opens_it_modally_and_restores_focus(self):
        self.assertIn("showModal", self.page.js)
        self.assertIn("focus", self.page.js)

    def test_form_closes_the_dialog(self):
        dialog = self.page.first("dialog")
        forms = [e for e in dialog.walk() if e.tag == "form"]
        self.assertTrue(forms)
        self.assertEqual(forms[0].get("method"), "dialog")

    def test_email_is_labelled_and_required(self):
        field = self.page.first("input", type="email")
        self.assertIsNotNone(field)
        self.assertTrue(self.page.label_of(field))
        self.assertTrue(field.has("required"))

    def test_cancel_and_subscribe_buttons_are_named(self):
        dialog = self.page.first("dialog")
        names = [self.page.name_of(b).lower() for b in dialog.walk() if b.tag == "button"]
        self.assertIn("cancel", names)
        self.assertIn("subscribe", names)

    def test_focus_is_visible_and_layout_fluid(self):
        self.assertTrue(self.page.has_focus_style())
        self.assertTrue(self.page.is_fluid())
