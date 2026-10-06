class _Subscription:
    __slots__ = ("callback", "active")

    def __init__(self, callback):
        self.callback = callback
        self.active = True


class Broker:
    """Topic based publish/subscribe."""

    def __init__(self):
        self._subs = {}

    def subscribe(self, topic, callback):
        sub = _Subscription(callback)
        self._subs.setdefault(topic, []).append(sub)

        def unsubscribe():
            if not sub.active:
                return
            sub.active = False
            live = self._subs.get(topic, [])
            live[:] = [s for s in live if s is not sub]

        return unsubscribe

    def publish(self, topic, message):
        called = 0
        for sub in list(self._subs.get(topic, [])):
            if not sub.active:
                continue
            sub.callback(message)
            called += 1
        return called
