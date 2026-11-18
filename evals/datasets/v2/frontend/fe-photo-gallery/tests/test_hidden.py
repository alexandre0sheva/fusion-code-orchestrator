import os
import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def figures(self):
        return self.page.find("figure")

    def test_six_figures_with_photos_and_captions(self):
        figures = self.figures()
        self.assertGreaterEqual(len(figures), 6)
        for figure in figures:
            inner = figure.walk()
            images = [e for e in inner if e.tag == "img"]
            captions = [e for e in inner if e.tag == "figcaption"]
            self.assertTrue(images and captions and captions[0].text)
            self.assertTrue(images[0].get("alt").strip())

    def test_every_photo_file_is_used_and_exists(self):
        sources = {e.get("src") for f in self.figures() for e in f.walk() if e.tag == "img"}
        for n in range(1, 7):
            self.assertIn(f"photos/p{n}.svg", sources)
        for source in sources:
            self.assertTrue(os.path.exists(source), source)

    def test_photos_reserve_space_and_load_lazily(self):
        for figure in self.figures():
            img = next(e for e in figure.walk() if e.tag == "img")
            self.assertTrue(img.get("width") and img.get("height"))
            self.assertEqual(img.get("loading"), "lazy")

    def test_a_responsive_grid(self):
        self.assertTrue(any("grid" in value for _, value in self.page.declared("display")))
        css = self.page.css
        self.assertTrue(self.page.media_queries() or "auto-fit" in css or "auto-fill" in css)
        self.assertTrue(self.page.is_fluid())

    def test_each_photo_has_a_view_larger_button(self):
        buttons = [b for b in self.page.find("button") if "view" in self.page.name_of(b).lower()]
        self.assertGreaterEqual(len(buttons), 6)

    def test_a_named_dialog_viewer_with_a_close_button(self):
        dialog = self.page.first("dialog", id="viewer")
        self.assertIsNotNone(dialog)
        labelled = dialog.get("aria-labelledby")
        self.assertTrue(dialog.get("aria-label").strip() or (labelled and self.page.by_id(labelled)))
        names = [self.page.name_of(e).lower() for e in dialog.walk() if e.tag == "button"]
        self.assertTrue(any("close" in n for n in names))

    def test_the_viewer_opens_modally_from_a_script(self):
        self.assertTrue(self.page.find("script", src="app.js"))
        self.assertIn("showModal", self.page.js)

    def test_one_h1_a_main_landmark_and_focus_style(self):
        self.assertEqual(len([h for h in self.page.headings() if h[0] == 1]), 1)
        self.assertTrue(self.page.find("main"))
        self.assertTrue(self.page.has_focus_style())
