import unittest

from retry import retry


class RetryTests(unittest.TestCase):
    def test_waits_grow_and_none_after_the_last_attempt(self):
        waits = []

        @retry(attempts=3, delay=1.0, backoff=2.0, sleep=waits.append)
        def always_fails():
            raise RuntimeError("no")

        with self.assertRaises(RuntimeError):
            always_fails()
        self.assertEqual(waits, [1.0, 2.0])

    def test_success_on_second_attempt(self):
        waits, calls = [], []

        @retry(attempts=4, delay=0.5, sleep=waits.append)
        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("first")
            return "ok"

        self.assertEqual(flaky(), "ok")
        self.assertEqual(waits, [0.5])
        self.assertEqual(len(calls), 2)

    def test_the_last_exception_is_raised(self):
        counter = []

        @retry(attempts=3, sleep=lambda s: None)
        def fails():
            counter.append(1)
            raise RuntimeError(f"failure {len(counter)}")

        with self.assertRaises(RuntimeError) as ctx:
            fails()
        self.assertEqual(str(ctx.exception), "failure 3")

    def test_other_exceptions_propagate_at_once(self):
        waits, calls = [], []

        @retry(attempts=5, exceptions=(ValueError,), sleep=waits.append)
        def wrong_kind():
            calls.append(1)
            raise KeyError("not retried")

        with self.assertRaises(KeyError):
            wrong_kind()
        self.assertEqual((waits, len(calls)), ([], 1))

    def test_single_attempt_never_sleeps(self):
        waits = []

        @retry(attempts=1, sleep=waits.append)
        def fails():
            raise RuntimeError("x")

        with self.assertRaises(RuntimeError):
            fails()
        self.assertEqual(waits, [])

    def test_arguments_are_passed_through(self):
        @retry(attempts=2, sleep=lambda s: None)
        def add(a, b=0):
            return a + b

        self.assertEqual(add(2, b=3), 5)

    def test_metadata_is_kept(self):
        @retry()
        def documented():
            """Original docstring."""

        self.assertEqual(documented.__name__, "documented")
        self.assertEqual(documented.__doc__, "Original docstring.")

    def test_attempts_must_be_positive(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                retry(attempts=bad)
