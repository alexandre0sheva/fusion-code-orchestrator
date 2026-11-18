import copy
import unittest

import config
from merging import merge


class MergeTests(unittest.TestCase):
    def test_nested_merge_keeps_siblings(self):
        base = {"server": {"host": "h", "port": 1, "tls": {"enabled": False}}}
        result = merge(base, {"server": {"port": 2, "tls": {"enabled": True}}})
        self.assertEqual(result, {"server": {"host": "h", "port": 2, "tls": {"enabled": True}}})

    def test_lists_are_replaced_not_merged(self):
        self.assertEqual(merge({"tags": [1, 2]}, {"tags": [3]}), {"tags": [3]})

    def test_scalar_replaces_dict_and_dict_replaces_scalar(self):
        self.assertEqual(merge({"a": {"b": 1}}, {"a": 5}), {"a": 5})
        self.assertEqual(merge({"a": 5}, {"a": {"b": 1}}), {"a": {"b": 1}})

    def test_none_removes_a_key_at_any_depth(self):
        base = {"a": 1, "b": {"c": 2, "d": 3}}
        self.assertEqual(merge(base, {"a": None, "b": {"c": None}}), {"b": {"d": 3}})

    def test_none_for_a_missing_key_adds_nothing(self):
        self.assertEqual(merge({"a": 1}, {"z": None}), {"a": 1})

    def test_arguments_are_not_modified(self):
        base = {"a": {"b": 1}, "l": [1]}
        override = {"a": {"c": 2}}
        base_before, override_before = copy.deepcopy(base), copy.deepcopy(override)
        merge(base, override)
        self.assertEqual(base, base_before)
        self.assertEqual(override, override_before)

    def test_result_shares_nothing_with_the_inputs(self):
        base = {"a": {"b": [1]}, "keep": {"x": [1]}}
        override = {"new": {"y": [2]}, "a": {"c": {"z": [3]}}}
        result = merge(base, override)
        result["a"]["b"].append(99)
        result["keep"]["x"].append(99)
        result["new"]["y"].append(99)
        result["a"]["c"]["z"].append(99)
        self.assertEqual(base, {"a": {"b": [1]}, "keep": {"x": [1]}})
        self.assertEqual(override, {"new": {"y": [2]}, "a": {"c": {"z": [3]}}})

    def test_empty_inputs(self):
        self.assertEqual(merge({}, {}), {})
        self.assertEqual(merge({"a": 1}, {}), {"a": 1})


class LoadConfigTests(unittest.TestCase):
    def test_defaults_are_loaded(self):
        self.assertEqual(config.load_config()["server"]["port"], 8000)

    def test_override_keeps_the_rest_of_the_section(self):
        result = config.load_config({"server": {"port": 9000}})
        self.assertEqual(result["server"]["host"], "localhost")
        self.assertEqual(result["server"]["port"], 9000)

    def test_defaults_are_never_changed(self):
        before = copy.deepcopy(config.DEFAULTS)
        result = config.load_config({"debug": True, "server": {"tls": {"enabled": True}}})
        result["tags"].append("mutated")
        result["server"]["host"] = "elsewhere"
        self.assertEqual(config.DEFAULTS, before)
        self.assertEqual(config.load_config()["tags"], ["base"])
