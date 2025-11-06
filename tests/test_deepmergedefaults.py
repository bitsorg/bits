import unittest
import yaml
import bits_helpers.utilities
from bits_helpers.utilities import merge_dicts

class DeepMergeTest(unittest.TestCase):
    # Test overwriting existing top-level keys from dict1 with top-level keys from dict2.
    # Test adding new top level keys from dict2.
    def test_flat_merge(self):
        d1 = ({"a": 1, "b": 2})
        d2 = ({"b": 3, "c": 4})
        result = merge_dicts(d1, d2)
        self.assertEqual(result, {"a": 1, "b": 3, "c": 4})

    # Test nested merge of dicts within dicts
    def test_nested_merge(self):
        d1 = {
            "a": 1,
            "b": {"x": 1, "y": 2},
        }
        d2 = {
            "b": {"y": 3, "z": 4},
            "c": 5,
        }
        result = merge_dicts(d1, d2)
        expected = {"a": 1, "b": {"x": 1, "y": 3, "z": 4}, "c": 5}
        self.assertEqual(result, expected)

    # Test merging of lists
    def test_list_merge(self):
        d1 = {"disabled": ["pkg1", "pkg2"]}
        d2 = {"disabled": ["pkg3"]}
        result = merge_dicts(d1, d2)
        self.assertEqual(result, {"disabled": ["pkg1", "pkg2", "pkg3"]})

    # Test non-recursive overwrite on type mismatch between dict and non-dict
    def test_overwrite_non_dict(self):
        d1 = {"a": {"nested": 1}}
        d2 = {"a": 42}
        result = merge_dicts(d1, d2)
        self.assertEqual(result, {"a": 42})

    # Test merging of non-overlapping dictionaries
    def test_new_key_added(self):
        d1 = {"a": 1}
        d2 = {"b": 2}
        result = merge_dicts(d1, d2)
        self.assertEqual(result, {"a": 1, "b": 2})

    # Test merging of empty dictionaries
    def test_empty_dicts(self):
        d1 = {}
        d2 = {}
        result = merge_dicts(d1, d2)
        self.assertEqual(result, {})

    # Test merging a non-empty first with empty second dict
    def test_merge_with_empty_second(self):
        d1 = {"a": 1}
        d2 = {}
        result = merge_dicts(d1, d2)
        self.assertEqual(result, {"a": 1})

    # Test merging a empty first with non-empty second dict
    def test_merge_with_empty_first(self):
        d1 = {}
        d2 = {"b": 2}
        result = merge_dicts(d1, d2)
        self.assertEqual(result, {"b": 2})

    # Test type mismatch between list and non-list
    def test_list_non_list_conflict(self):
        """When one side is list and the other is not, prefer dict2."""
        d1 = {"disabled": ["pkg1", "pkg2"]}
        d2 = {"disabled": "notalist"}
        result = merge_dicts(d1, d2)
        self.assertEqual(result, {"disabled": "notalist"})
