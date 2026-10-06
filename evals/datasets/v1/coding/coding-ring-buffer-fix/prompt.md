`RingBuffer` in `ringbuffer.py` is a fixed-size queue. Reported problems: appending to a full buffer raises `OverflowError` instead of overwriting, and after a few `pop()` calls the buffer returns garbage and its length never shrinks.

Fix it so that:

- `RingBuffer(capacity)` needs `capacity >= 1`, else `ValueError`.
- `append(item)` adds the newest item; when the buffer is full it **overwrites the oldest** item.
- `pop()` removes and returns the oldest item (`IndexError` when empty); `peek()` returns it without removing (`IndexError` when empty).
- `to_list()` returns the items from oldest to newest; `len(buffer)` is the number of items; `is_full()` says whether it holds `capacity` items; `clear()` empties it.
- It keeps working after any number of appends and pops (the indexes wrap around).
