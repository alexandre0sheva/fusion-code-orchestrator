Implement the class `EventEmitter` in `emitter.py`.

- `on(event, handler)` registers `handler` for `event`. Handlers run in registration order. The same handler registered twice runs twice.
- `once(event, handler)` registers a handler that runs at most once: it is removed just *before* it is called, so a nested `emit` of the same event from inside it does not call it again.
- `off(event, handler)` removes the earliest registration of `handler` for `event` (a `once` registration counts). Removing a handler that is not registered does nothing.
- `emit(event, *args, **kwargs)` calls each registered handler with those arguments and returns how many handlers it called (`0` when there are none). The set of handlers is fixed when `emit` starts: handlers a handler adds are called from the next `emit`, and handlers it removes still run in this one.
- If a handler raises, the remaining handlers still run; after all have run, the **first** exception raised is re-raised.
