class _Entry:
    __slots__ = ("handler", "once")

    def __init__(self, handler, once: bool) -> None:
        self.handler = handler
        self.once = once


class EventEmitter:
    """A small synchronous event emitter."""

    def __init__(self) -> None:
        self._entries: dict = {}

    def on(self, event, handler) -> None:
        self._entries.setdefault(event, []).append(_Entry(handler, False))

    def once(self, event, handler) -> None:
        self._entries.setdefault(event, []).append(_Entry(handler, True))

    def off(self, event, handler) -> None:
        entries = self._entries.get(event, [])
        for index, entry in enumerate(entries):
            if entry.handler == handler:
                del entries[index]
                return

    def emit(self, event, *args, **kwargs) -> int:
        snapshot = list(self._entries.get(event, []))
        first_error = None
        called = 0
        for entry in snapshot:
            if entry.once:
                live = self._entries.get(event, [])
                if not any(e is entry for e in live):
                    continue
                live[:] = [e for e in live if e is not entry]
            called += 1
            try:
                entry.handler(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
        return called
