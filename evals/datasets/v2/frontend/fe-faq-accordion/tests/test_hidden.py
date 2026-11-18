import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_five_expandable_questions(self):
        self.assertGreaterEqual(len(self.page.find("details")), 5)

    def test_each_question_has_a_summary_and_an_answer(self):
        for item in self.page.find("details"):
            summary = [e for e in item.children if e.tag == "summary"]
            self.assertTrue(summary and summary[0].text)
            answer = [e for e in item.children if e.tag == "p"]
            self.assertTrue(answer and len(answer[0].text) > 20)

    def test_works_without_script_or_click_handlers(self):
        self.assertNotIn("onclick", self.page.html.lower())
        self.assertEqual(self.page.find("script"), [])

    def test_one_h1_and_a_main_landmark(self):
        self.assertEqual(len([h for h in self.page.headings() if h[0] == 1]), 1)
        self.assertTrue(self.page.find("main"))

    def test_summaries_show_keyboard_focus(self):
        self.assertTrue(self.page.has_focus_style())

    def test_fluid_layout(self):
        self.assertTrue(self.page.is_fluid())
