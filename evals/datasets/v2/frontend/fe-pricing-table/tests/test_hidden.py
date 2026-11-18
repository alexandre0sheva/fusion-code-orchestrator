import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_three_plans_with_names_and_prices(self):
        plans = self.page.find("article")
        self.assertEqual(len(plans), 3)
        for plan in plans:
            headings = [e for e in plan.walk() if e.tag == "h2"]
            self.assertTrue(headings and headings[0].text)
            self.assertTrue(any("$" in e.text for e in plan.walk() if e.tag == "p"))

    def test_each_plan_lists_at_least_three_features(self):
        for plan in self.page.find("article"):
            items = [e for e in plan.walk() if e.tag == "li"]
            self.assertGreaterEqual(len(items), 3)

    def test_choose_links_name_their_plan(self):
        for plan in self.page.find("article"):
            name = [e for e in plan.walk() if e.tag == "h2"][0].text.lower()
            links = [e for e in plan.walk() if e.tag == "a"]
            self.assertTrue(links)
            self.assertIn(name, self.page.name_of(links[0]).lower())

    def test_exactly_one_recommended_plan(self):
        self.assertEqual(self.page.text().lower().count("recommended"), 1)

    def test_the_prices_are_the_briefed_ones(self):
        text = self.page.text()
        for price in ("$9", "$29", "$99"):
            self.assertIn(price, text)

    def test_cards_adapt_to_the_screen(self):
        self.assertTrue(self.page.is_fluid())

    def test_heading_levels(self):
        levels = [level for level, _ in self.page.headings()]
        self.assertEqual(levels[0], 1)
        for before, after in zip(levels, levels[1:]):
            self.assertLessEqual(after, before + 1)
