import unittest

from globbing import glob_match


class GlobTests(unittest.TestCase):
    def test_literal_and_case(self):
        self.assertTrue(glob_match("main.py", "main.py"))
        self.assertFalse(glob_match("main.py", "Main.py"))
        self.assertFalse(glob_match("main.py", "main.pyc"))
        self.assertTrue(glob_match("a.b", "a.b"))
        self.assertFalse(glob_match("a.b", "axb"))

    def test_star_does_not_cross_slashes(self):
        self.assertTrue(glob_match("*.py", "main.py"))
        self.assertTrue(glob_match("*.py", ".py"))
        self.assertFalse(glob_match("*.py", "pkg/main.py"))
        self.assertTrue(glob_match("src/*/main.py", "src/app/main.py"))
        self.assertFalse(glob_match("src/*/main.py", "src/app/sub/main.py"))

    def test_double_star_crosses_slashes(self):
        self.assertTrue(glob_match("src/**/main.py", "src/app/sub/main.py"))
        self.assertTrue(glob_match("**.py", "a/b/c.py"))
        self.assertTrue(glob_match("a**z", "az"))

    def test_question_mark(self):
        self.assertTrue(glob_match("a?c", "abc"))
        self.assertFalse(glob_match("a?c", "ac"))
        self.assertFalse(glob_match("a?c", "a/c"))
        self.assertFalse(glob_match("a?c", "abbc"))

    def test_classes_and_ranges(self):
        self.assertTrue(glob_match("[abc].txt", "b.txt"))
        self.assertFalse(glob_match("[abc].txt", "d.txt"))
        self.assertTrue(glob_match("file[0-9]", "file7"))
        self.assertFalse(glob_match("file[0-9]", "filex"))

    def test_negated_class(self):
        self.assertTrue(glob_match("[!a-c]x", "dx"))
        self.assertFalse(glob_match("[!a-c]x", "bx"))

    def test_classes_never_match_a_slash(self):
        self.assertFalse(glob_match("a[!x]b", "a/b"))
        self.assertFalse(glob_match("a[/x]b", "a/b"))

    def test_escapes(self):
        self.assertTrue(glob_match("\\*", "*"))
        self.assertFalse(glob_match("\\*", "abc"))
        self.assertTrue(glob_match("a\\?b", "a?b"))
        self.assertFalse(glob_match("a\\?b", "axb"))
        self.assertTrue(glob_match("\\[x]", "[x]"))

    def test_regex_characters_are_literal(self):
        self.assertTrue(glob_match("a+b(c)", "a+b(c)"))
        self.assertFalse(glob_match("a+b", "aab"))
        self.assertTrue(glob_match("^$", "^$"))

    def test_bad_patterns(self):
        for pattern in ("[abc", "[]", "[!]", "[z-a]", "abc\\"):
            with self.assertRaises(ValueError, msg=pattern):
                glob_match(pattern, "x")
