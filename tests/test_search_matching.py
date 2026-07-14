import unittest

from utils.search_utils import lookup_text_index


class SearchMatchingTests(unittest.TestCase):
    def test_contains_matching_returns_indexed_values(self):
        index = {
            "ods.user_daily": ["task-a"],
            "dwd.order_daily": ["task-b"],
        }
        self.assertEqual(lookup_text_index("user", index), {"task-a"})

    def test_prefix_matching_returns_all_matching_values(self):
        index = {
            "ods.user_daily": ["task-a"],
            "ods.user_monthly": ["task-b"],
        }
        self.assertEqual(lookup_text_index("user_", index), {"task-a", "task-b"})


if __name__ == "__main__":
    unittest.main()
