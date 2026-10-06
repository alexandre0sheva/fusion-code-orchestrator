`merging.py` and `config.py` build the application's settings. Two bugs are reported: overriding `server.port` throws away `server.host` and the rest of the `server` section, and after one call the shared `DEFAULTS` have changed for every later caller.

Fix them so that:

- `merge(base, override)` returns a **new** dict. Dict values present on both sides are merged recursively; any other value in `override` (lists included) replaces the one in `base`; keys only in `base` are kept.
- A value of `None` in `override` removes that key from the result (at any depth).
- Neither argument is modified, and the result shares no mutable nested object (dict or list) with either argument.
- `load_config(overrides=None)` returns the defaults merged with the overrides and leaves `DEFAULTS` untouched.
