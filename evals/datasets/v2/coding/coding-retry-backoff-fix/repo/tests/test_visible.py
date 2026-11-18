import unittest

from retry import retry


class VisibleTests(unittest.TestCase):
    def test_returns_the_value(self):
        @retry(attempts=2, sleep=lambda s: None)
        def ok():
            return 5

        self.assertEqual(ok(), 5)

    def test_retries_until_success(self):
        calls = []

        @retry(attempts=3, sleep=lambda s: None)
        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("again")
            return "done"

        self.assertEqual(flaky(), "done")

    def test_single_attempt_never_sleeps(self):
        waits = []

        @retry(attempts=1, sleep=waits.append)
        def fails():
            raise RuntimeError("x")

        with self.assertRaises(RuntimeError):
            fails()
        self.assertEqual(waits, [])
