import unittest

from pubsub import Broker


class BrokerTests(unittest.TestCase):
    def test_topics_are_separate_and_count_is_returned(self):
        seen = []
        broker = Broker()
        broker.subscribe("a", seen.append)
        self.assertEqual(broker.publish("b", 1), 0)
        self.assertEqual(broker.publish("a", 2), 1)
        self.assertEqual(seen, [2])

    def test_unsubscribe_twice_is_harmless(self):
        broker = Broker()
        off = broker.subscribe("t", lambda m: None)
        off()
        off()
        self.assertEqual(broker.publish("t", 1), 0)

    def test_same_callback_twice_is_two_subscriptions(self):
        seen = []
        broker = Broker()
        first = broker.subscribe("t", seen.append)
        second = broker.subscribe("t", seen.append)
        self.assertEqual(broker.publish("t", "x"), 2)
        second()
        self.assertEqual(broker.publish("t", "y"), 1)
        first()
        self.assertEqual(broker.publish("t", "z"), 0)
        self.assertEqual(seen, ["x", "x", "y"])

    def test_unsubscribing_the_second_of_two_equal_callbacks_keeps_the_first_position(self):
        order = []
        broker = Broker()

        def cb(m):
            order.append(("cb", m))

        broker.subscribe("t", cb)
        broker.subscribe("t", lambda m: order.append(("other", m)))
        off_second = broker.subscribe("t", cb)
        off_second()
        broker.publish("t", 1)
        self.assertEqual(order, [("cb", 1), ("other", 1)])

    def test_self_unsubscribe_does_not_skip_the_next_subscriber(self):
        seen = []
        broker = Broker()
        holder = {}

        def once(m):
            seen.append(("once", m))
            holder["off"]()

        holder["off"] = broker.subscribe("t", once)
        broker.subscribe("t", lambda m: seen.append(("next", m)))
        self.assertEqual(broker.publish("t", 1), 2)
        self.assertEqual(broker.publish("t", 2), 1)
        self.assertEqual(seen, [("once", 1), ("next", 1), ("next", 2)])

    def test_unsubscribing_a_later_subscriber_prevents_its_call(self):
        seen = []
        broker = Broker()
        holder = {}
        broker.subscribe("t", lambda m: (seen.append("first"), holder["off"]()))
        holder["off"] = broker.subscribe("t", lambda m: seen.append("second"))
        self.assertEqual(broker.publish("t", 1), 1)
        self.assertEqual(seen, ["first"])

    def test_a_subscriber_added_during_publish_waits_for_the_next_one(self):
        seen = []
        broker = Broker()

        def adder(m):
            seen.append("adder")
            broker.subscribe("t", lambda m2: seen.append("late"))

        off = broker.subscribe("t", adder)
        self.assertEqual(broker.publish("t", 1), 1)
        off()
        self.assertEqual(broker.publish("t", 2), 1)
        self.assertEqual(seen, ["adder", "late"])

    def test_an_exception_stops_the_publish(self):
        seen = []
        broker = Broker()

        def boom(m):
            raise RuntimeError("boom")

        broker.subscribe("t", lambda m: seen.append("a"))
        broker.subscribe("t", boom)
        broker.subscribe("t", lambda m: seen.append("c"))
        with self.assertRaises(RuntimeError):
            broker.publish("t", 1)
        self.assertEqual(seen, ["a"])
