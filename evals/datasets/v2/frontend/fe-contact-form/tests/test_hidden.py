import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_every_control_has_a_label(self):
        self.assertEqual(self.page.unlabelled_controls(), [])

    def test_name_and_email_help_autofill(self):
        self.assertEqual(self.page.first("input", name="name").get("autocomplete"), "name")
        email = self.page.first("input", type="email")
        self.assertEqual(email.get("autocomplete"), "email")
        self.assertTrue(email.has("required"))

    def test_radio_group_has_a_legend(self):
        sets = self.page.find("fieldset")
        self.assertTrue(sets)
        legend = [e for e in sets[0].children if e.tag == "legend"]
        self.assertTrue(legend and legend[0].text)
        radios = [e for e in sets[0].walk() if e.tag == "input" and e.get("type") == "radio"]
        self.assertEqual(len(radios), 3)
        self.assertEqual(len({r.get("name") for r in radios}), 1)

    def test_message_box_has_a_limit(self):
        box = self.page.first("textarea")
        self.assertIsNotNone(box)
        self.assertTrue(box.has("required"))
        self.assertEqual(box.get("maxlength"), "1000")

    def test_send_button(self):
        self.assertTrue(any("send" in self.page.name_of(b).lower() for b in self.page.find("button")))

    def test_form_fits_small_screens(self):
        self.assertTrue(self.page.is_fluid())
        self.assertEqual(len([h for h in self.page.headings() if h[0] == 1]), 1)
