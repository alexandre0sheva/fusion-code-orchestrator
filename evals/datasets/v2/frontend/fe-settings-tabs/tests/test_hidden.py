import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def tabs(self):
        return self.page.find(role="tab")

    def test_tablist_has_an_accessible_name(self):
        tablist = self.page.first("div", role="tablist") or (self.page.find(role="tablist") or [None])[0]
        self.assertIsNotNone(tablist)
        self.assertTrue(tablist.get("aria-label").strip() or tablist.get("aria-labelledby"))

    def test_three_tabs_are_buttons(self):
        tabs = self.tabs()
        self.assertEqual(len(tabs), 3)
        self.assertTrue(all(t.tag == "button" for t in tabs))
        names = [self.page.name_of(t).lower() for t in tabs]
        self.assertEqual(names, ["profile", "notifications", "privacy"])

    def test_each_tab_controls_a_panel_labelled_by_it(self):
        for tab in self.tabs():
            panel = self.page.by_id(tab.get("aria-controls"))
            self.assertIsNotNone(panel, tab.get("aria-controls"))
            self.assertEqual(panel.get("role"), "tabpanel")
            self.assertTrue(tab.get("id"))
            self.assertEqual(panel.get("aria-labelledby"), tab.get("id"))

    def test_the_first_tab_is_selected_on_load(self):
        selected = [t.get("aria-selected") for t in self.tabs()]
        self.assertEqual(selected, ["true", "false", "false"])

    def test_only_the_active_panel_is_shown(self):
        panels = [self.page.by_id(t.get("aria-controls")) for t in self.tabs()]
        self.assertFalse(panels[0].has("hidden"))
        self.assertTrue(all(p.has("hidden") for p in panels[1:]))

    def test_a_script_switches_tabs_with_the_arrow_keys(self):
        self.assertTrue(self.page.find("script", src="app.js"))
        self.assertIn("aria-selected", self.page.js)
        self.assertIn("ArrowRight", self.page.js)
        self.assertIn("ArrowLeft", self.page.js)

    def test_every_setting_is_a_labelled_control(self):
        controls = self.page.find("input") + self.page.find("textarea") + self.page.find("select")
        self.assertGreaterEqual(len(controls), 5)
        self.assertEqual(self.page.unlabelled_controls(), [])

    def test_focus_style_and_fluid_layout(self):
        self.assertTrue(self.page.has_focus_style())
        self.assertTrue(self.page.is_fluid())
