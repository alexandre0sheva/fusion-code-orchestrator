import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_six_products_in_a_list(self):
        lists = self.page.find("ul")
        self.assertTrue(any(len([c for c in ul.children if c.tag == "li"]) >= 6 for ul in lists))

    def test_every_image_has_alt_text(self):
        images = self.page.find("img")
        self.assertGreaterEqual(len(images), 6)
        self.assertEqual(self.page.images_without_alt(), [])
        self.assertTrue(all(i.get("alt").strip() for i in images))

    def test_each_product_has_a_heading_a_price_and_a_button(self):
        cards = [e for e in self.page.find("li") if any(c.tag == "img" for c in e.walk())]
        self.assertGreaterEqual(len(cards), 6)
        for item in cards:
            tags = {e.tag for e in item.walk()}
            self.assertIn("h2", tags)
            self.assertIn("button", tags)
            self.assertIn("$", item.text)

    def test_buttons_say_which_product(self):
        buttons = self.page.find("button")
        names = {self.page.name_of(b).lower() for b in buttons}
        self.assertEqual(len(names), len(buttons))
        self.assertTrue(all("add" in n for n in names))

    def test_grid_adapts_without_a_media_query(self):
        self.assertTrue(self.page.is_fluid())
        self.assertTrue(any("grid" in d for _, d in self.page.declared("display")))

    def test_the_products_are_the_briefed_ones(self):
        text = self.page.text()
        for expected in ("Blue ceramic mug", "$14", "Wool throw blanket", "$68"):
            self.assertIn(expected, text)
