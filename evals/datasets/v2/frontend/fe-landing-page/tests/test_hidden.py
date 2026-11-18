import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_title_and_language(self):
        self.assertTrue(self.page.title.strip())
        self.assertTrue(self.page.lang.strip())

    def test_one_h1_and_a_free_trial_call_to_action(self):
        self.assertEqual(len([h for h in self.page.headings() if h[0] == 1]), 1)
        actions = self.page.find("a") + self.page.find("button")
        self.assertTrue(any("trial" in self.page.name_of(a).lower() for a in actions))

    def test_three_feature_cards_with_headings(self):
        self.assertGreaterEqual(len([h for h in self.page.headings() if h[0] == 3]), 3)

    def test_landmarks(self):
        for tag in ("header", "nav", "main", "footer"):
            self.assertTrue(self.page.find(tag), tag)
        self.assertTrue(self.page.first("nav").get("aria-label"))

    def test_navigation_has_three_named_links(self):
        links = [a for a in self.page.find("a") if a.ancestors() and any(p.tag == "nav" for p in a.ancestors())]
        self.assertGreaterEqual(len(links), 3)
        self.assertTrue(all(self.page.name_of(a) for a in links))

    def test_layout_adapts_to_small_screens(self):
        self.assertTrue(self.page.has_viewport_meta())
        self.assertTrue(self.page.is_fluid(), self.page.wide_fixed_widths())

    def test_headings_do_not_skip_levels(self):
        levels = [level for level, _ in self.page.headings()]
        for before, after in zip(levels, levels[1:]):
            self.assertLessEqual(after, before + 1)
