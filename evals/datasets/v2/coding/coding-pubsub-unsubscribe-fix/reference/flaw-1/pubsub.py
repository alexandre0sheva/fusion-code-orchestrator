class Broker:
    """Topic based publish/subscribe."""

    def __init__(self):
        self._subs = {}

    def subscribe(self, topic, callback):
        self._subs.setdefault(topic, []).append(callback)

        def unsubscribe():
            self._subs[topic].remove(callback)

        return unsubscribe

    def publish(self, topic, message):
        snapshot = list(self._subs.get(topic, []))
        for callback in snapshot:
            callback(message)
        return len(snapshot)
