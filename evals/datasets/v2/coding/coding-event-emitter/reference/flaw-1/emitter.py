class EventEmitter:
    """A small synchronous event emitter."""

    def __init__(self) -> None:
        self._handlers: dict = {}

    def on(self, event, handler) -> None:
        self._handlers.setdefault(event, []).append([handler, False])

    def once(self, event, handler) -> None:
        self._handlers.setdefault(event, []).append([handler, True])

    def off(self, event, handler) -> None:
        entries = self._handlers.get(event, [])
        for index, entry in enumerate(entries):
            if entry[0] == handler:
                del entries[index]
                return

    def emit(self, event, *args, **kwargs) -> int:
        entries = self._handlers.get(event, [])
        called = 0
        index = 0
        while index < len(entries):
            handler, once = entries[index]
            if once:
                del entries[index]
            else:
                index += 1
            called += 1
            handler(*args, **kwargs)
        return called
