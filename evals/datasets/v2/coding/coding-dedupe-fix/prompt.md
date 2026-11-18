`unique(items, key=None)` in `listutil.py` should return the items without duplicates, but it scrambles the order and crashes on lists and dicts.

Fix it:

- Keep the **first** occurrence of each item, in the original order.
- Two items are duplicates when they are equal. If `key` is given (a function), they are duplicates when `key(item)` values are equal; the item returned is the first one with that key.
- It must work for unhashable items (lists, dicts, sets), comparing them by equality, and for a mix of hashable and unhashable items.
- The input is not modified; the result is a new list. `unique([])` is `[]`.
