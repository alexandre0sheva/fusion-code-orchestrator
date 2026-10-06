Implement `pointer_get(doc, pointer)` and `pointer_set(doc, pointer, value)` in `jsonpointer.py`, following RFC 6901 (JSON Pointer) for documents made of dicts and lists.

- A pointer is `""` (the whole document) or a string of `/`-separated tokens that starts with `/`. A non-empty pointer that does not start with `/` raises `ValueError`.
- Inside a token, `~1` means `/` and `~0` means `~`. Decode `~1` first, then `~0`, so `~01` is the text `~1`. The pointer `"/"` has one token, the empty string, which is a valid dict key.
- A dict token is a key. A list token must be `0` or digits with no leading zero; `-` and anything else is not an index.
- `pointer_get` returns the value. Anything that cannot be resolved (missing key, bad or out-of-range index, stepping into a string or number) raises `PointerError`, which is defined in `jsonpointer.py` and is a `LookupError`.
- `pointer_set` changes the document in place and returns it. The parent of the last token must already exist, else `PointerError`. In a dict the key is set or replaced. In a list an index at most `len(list)` sets or replaces that item (an index equal to the length appends), and `-` appends. Setting the whole document (`""`) raises `ValueError`.
