from merging import merge

DEFAULTS = {
    "server": {"host": "localhost", "port": 8000, "tls": {"enabled": False}},
    "debug": False,
    "tags": ["base"],
}


def load_config(overrides=None):
    """The settings: defaults with ``overrides`` applied."""
    return merge(DEFAULTS, overrides or {})
