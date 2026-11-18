class _Subscription:
    def __init__(self, callback):
        self.callback = callback


class Broker:
    """Topic based publish/subscribe."""

    def __init__(self):
        self._subs = {}

    def subscribe(self, topic, callback):
        sub = _Subscription(callback)
        self._subs.setdefault(topic, []).append(sub)

        def unsubscribe():
            live = self._subs.get(topic, [])
            if sub in live:
                live.remove(sub)

        return unsubscribe

    def publish(self, topic, message):
        called = 0
        for sub in self._subs.get(topic, []):
            sub.callback(message)
            called += 1
        return called
