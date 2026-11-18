class EventEmitter:
    """A small synchronous event emitter."""

    def __init__(self) -> None:
        self._handlers: dict = {}

    def on(self, event, handler) -> None:
        self._handlers.setdefault(event, []).append((handler, False))

    def once(self, event, handler) -> None:
        self._handlers.setdefault(event, []).append((handler, True))

    def off(self, event, handler) -> None:
        entries = self._handlers.get(event, [])
        for index, (h, _) in enumerate(entries):
            if h == handler:
                del entries[index]
                return

    def emit(self, event, *args, **kwargs) -> int:
        snapshot = list(self._handlers.get(event, []))
        first_error = None
        for entry in snapshot:
            handler, once = entry
            try:
                handler(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                first_error = first_error or exc
            if once and entry in self._handlers.get(event, []):
                self._handlers[event].remove(entry)
        if first_error is not None:
            raise first_error
        return len(snapshot)
