import unittest

from diffstat import diffstat


class DiffstatTests(unittest.TestCase):
    def test_headers_do_not_count(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n keep\n-old\n+new\n"
        self.assertEqual(diffstat(diff), {"files": 1, "added": 1, "removed": 1})

    def test_several_files_and_hunks(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "index 111..222 100644\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,3 +1,4 @@\n"
            " one\n"
            "+two\n"
            " three\n"
            " four\n"
            "@@ -10,2 +11,1 @@\n"
            " ten\n"
            "-eleven\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1 +1,2 @@\n"
            " x\n"
            "+y\n"
        )
        self.assertEqual(diffstat(diff), {"files": 2, "added": 2, "removed": 1})

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

    def test_new_and_deleted_files(self):
        diff = (
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+a\n"
            "+b\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,3 +0,0 @@\n"
            "-x\n"
            "-y\n"
            "-z\n"
        )
        self.assertEqual(diffstat(diff), {"files": 2, "added": 2, "removed": 3})

    def test_no_newline_marker_is_ignored(self):
        diff = (
            "--- a/f\n"
            "+++ b/f\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "\\ No newline at end of file\n"
            "+new\n"
            "\\ No newline at end of file\n"
        )
        self.assertEqual(diffstat(diff), {"files": 1, "added": 1, "removed": 1})

    def test_the_same_file_twice_counts_once(self):
        diff = (
            "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n"
            "--- a/f\n+++ b/f\n@@ -9 +9 @@\n-c\n+d\n"
        )
        self.assertEqual(diffstat(diff), {"files": 1, "added": 2, "removed": 2})

    def test_empty_diff(self):
        self.assertEqual(diffstat(""), {"files": 0, "added": 0, "removed": 0})
