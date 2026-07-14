import unittest

from utils.sql_utils import enforce_limit


class QueryLimitTests(unittest.TestCase):
    def test_oversized_user_limit_is_reduced_to_requested_limit(self):
        sql = enforce_limit("SELECT * FROM demo WHERE etl_date = '2026-01-01' LIMIT 100000", 20, 1000)
        self.assertRegex(sql, r"LIMIT\s+20\s*;?$")

    def test_smaller_existing_limit_is_preserved(self):
        sql = enforce_limit("SELECT * FROM demo LIMIT 5", 20, 1000)
        self.assertRegex(sql, r"LIMIT\s+5\s*;?$")

    def test_query_without_limit_gets_one(self):
        sql = enforce_limit("SELECT * FROM demo", 20, 1000)
        self.assertRegex(sql, r"LIMIT\s+20$")

    def test_refresh_is_not_given_a_limit(self):
        sql = enforce_limit("REFRESH TABLE demo", 20, 1000)
        self.assertEqual(sql, "REFRESH TABLE demo")
