class EventEmitter:
    """A small synchronous event emitter."""

    def on(self, event, handler) -> None:
        raise NotImplementedError

    def once(self, event, handler) -> None:
        raise NotImplementedError

    def off(self, event, handler) -> None:
        raise NotImplementedError

    def emit(self, event, *args, **kwargs) -> int:
        raise NotImplementedError
