class Broker:
    """Topic based publish/subscribe."""

    def __init__(self):
        self._subs = {}

    def subscribe(self, topic, callback):
        self._subs.setdefault(topic, []).append(callback)
        return lambda: self._subs[topic].remove(callback)

    def publish(self, topic, message):
        for callback in self._subs.get(topic, []):
            callback(message)
        return len(self._subs.get(topic, []))
