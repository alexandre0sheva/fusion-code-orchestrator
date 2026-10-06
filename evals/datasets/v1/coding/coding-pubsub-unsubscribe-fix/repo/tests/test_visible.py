import unittest

from pubsub import Broker


class VisibleTests(unittest.TestCase):
    def test_delivers_in_order(self):
        seen = []
        broker = Broker()
        broker.subscribe("t", lambda m: seen.append(("a", m)))
        broker.subscribe("t", lambda m: seen.append(("b", m)))
        self.assertEqual(broker.publish("t", 1), 2)
        self.assertEqual(seen, [("a", 1), ("b", 1)])

    def test_unsubscribe(self):
        seen = []
        broker = Broker()
        off = broker.subscribe("t", seen.append)
        off()
        broker.publish("t", 1)
        self.assertEqual(seen, [])

    def test_unsubscribe_twice_is_harmless(self):
        broker = Broker()
        off = broker.subscribe("t", lambda m: None)
        off()
        off()
        self.assertEqual(broker.publish("t", 1), 0)
