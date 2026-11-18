import unittest

from diffstat import diffstat

SIMPLE = (
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -1,2 +1,2 @@\n"
    " keep\n"
    "-old\n"
    "+new\n"
)


class VisibleTests(unittest.TestCase):
    def test_simple_diff(self):
        self.assertEqual(diffstat(SIMPLE), {"files": 1, "added": 1, "removed": 1})

    def test_empty(self):
        self.assertEqual(diffstat(""), {"files": 0, "added": 0, "removed": 0})

    def test_content_lines_that_look_like_headers_are_counted(self):
        diff = (
            "--- a/q.sql\n"
            "+++ b/q.sql\n"
            "@@ -1,3 +1,3 @@\n"
            " select 1;\n"
            "--- note\n"
            "+++ other note\n"
            " select 2;\n"
        )
        self.assertEqual(diffstat(diff), {"files": 1, "added": 1, "removed": 1})
