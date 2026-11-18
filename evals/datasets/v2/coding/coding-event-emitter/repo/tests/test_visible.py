import unittest

from emitter import EventEmitter


class VisibleTests(unittest.TestCase):
    def test_calls_in_order(self):
        seen = []
        bus = EventEmitter()
        bus.on("x", lambda v: seen.append(("a", v)))
        bus.on("x", lambda v: seen.append(("b", v)))
        self.assertEqual(bus.emit("x", 1), 2)
        self.assertEqual(seen, [("a", 1), ("b", 1)])

    def test_no_handlers(self):
        self.assertEqual(EventEmitter().emit("nothing"), 0)

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
