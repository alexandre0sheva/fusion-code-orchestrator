import unittest

from merging import merge
import copy
import config


class VisibleTests(unittest.TestCase):
    def test_adds_and_replaces_top_level_keys(self):
        self.assertEqual(merge({"a": 1}, {"b": 2, "a": 3}), {"a": 3, "b": 2})

    def test_nested_override_keeps_siblings(self):
        result = merge({"s": {"host": "h", "port": 1}}, {"s": {"port": 2}})
        self.assertEqual(result, {"s": {"host": "h", "port": 2}})

    def test_defaults_are_never_changed(self):
        before = copy.deepcopy(config.DEFAULTS)
        result = config.load_config({"debug": True, "server": {"tls": {"enabled": True}}})
        result["tags"].append("mutated")
        result["server"]["host"] = "elsewhere"
        self.assertEqual(config.DEFAULTS, before)
        self.assertEqual(config.load_config()["tags"], ["base"])
