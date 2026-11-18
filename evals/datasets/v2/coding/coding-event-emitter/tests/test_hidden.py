import unittest

from emitter import EventEmitter


class EventEmitterTests(unittest.TestCase):
    def test_order_arguments_and_count(self):
        seen = []
        bus = EventEmitter()
        bus.on("x", lambda *a, **k: seen.append(("a", a, k)))
        bus.on("x", lambda *a, **k: seen.append(("b", a, k)))
        self.assertEqual(bus.emit("x", 1, 2, key="v"), 2)
        self.assertEqual(seen, [("a", (1, 2), {"key": "v"}), ("b", (1, 2), {"key": "v"})])

    def test_events_are_separate(self):
        seen = []
        bus = EventEmitter()
        bus.on("a", lambda: seen.append("a"))
        self.assertEqual(bus.emit("b"), 0)
        self.assertEqual(seen, [])

    def test_once_runs_once(self):
        seen = []
        bus = EventEmitter()
        bus.once("x", lambda: seen.append(1))
        bus.emit("x")
        bus.emit("x")
        self.assertEqual(seen, [1])

    def test_once_is_removed_before_it_is_called(self):
        seen = []
        bus = EventEmitter()

        def handler():
            seen.append(1)
            if len(seen) < 3:
                bus.emit("x")

        bus.once("x", handler)
        bus.emit("x")
        self.assertEqual(seen, [1])

    def test_off_removes_the_earliest_registration(self):
        seen = []
        bus = EventEmitter()

        def h():
            seen.append("h")

        bus.on("x", h)
        bus.on("x", h)
        bus.off("x", h)
        bus.emit("x")
        self.assertEqual(seen, ["h"])

    def test_off_can_remove_a_once_handler(self):
        seen = []
        bus = EventEmitter()

        def h():
            seen.append("h")

        bus.once("x", h)
        bus.off("x", h)
        self.assertEqual(bus.emit("x"), 0)
        self.assertEqual(seen, [])

    def test_off_unknown_is_harmless(self):
        bus = EventEmitter()
        bus.off("x", lambda: None)
        bus.on("x", lambda: None)
        bus.off("x", lambda: None)
        self.assertEqual(bus.emit("x"), 1)

    def test_same_handler_twice_runs_twice(self):
        seen = []
        bus = EventEmitter()

        def h():
            seen.append(1)

        bus.on("x", h)
        bus.on("x", h)
        self.assertEqual(bus.emit("x"), 2)
        self.assertEqual(seen, [1, 1])

    def test_a_failing_handler_does_not_stop_the_rest(self):
        seen = []
        bus = EventEmitter()

        def boom():
            raise RuntimeError("first")

        def boom2():
            raise KeyError("second")

        bus.on("x", lambda: seen.append("a"))
        bus.on("x", boom)
        bus.on("x", boom2)
        bus.on("x", lambda: seen.append("d"))
        with self.assertRaises(RuntimeError):
            bus.emit("x")
        self.assertEqual(seen, ["a", "d"])

    def test_handlers_added_during_emit_wait_for_the_next_emit(self):
        seen = []
        bus = EventEmitter()

        def late():
            seen.append("late")

        def adder():
            seen.append("adder")
            bus.on("x", late)

        bus.on("x", adder)
        self.assertEqual(bus.emit("x"), 1)
        self.assertEqual(seen, ["adder"])
        bus.off("x", adder)
        self.assertEqual(bus.emit("x"), 1)
        self.assertEqual(seen, ["adder", "late"])

    def test_handlers_removed_during_emit_still_run_in_it(self):
        seen = []
        bus = EventEmitter()

        def second():
            seen.append("second")

        def first():
            seen.append("first")
            bus.off("x", second)

        bus.on("x", first)
        bus.on("x", second)
        bus.emit("x")
        self.assertEqual(seen, ["first", "second"])
        bus.emit("x")
        self.assertEqual(seen, ["first", "second", "first"])
