import sys
import types
import unittest

# 保持与现有测试一致的轻量环境兼容：生产环境由 requirements.txt 提供这些模块。
pyhive_stub = types.ModuleType("pyhive")
hive_stub = types.ModuleType("pyhive.hive")
hive_stub.Connection = object
pyhive_stub.hive = hive_stub
sys.modules.setdefault("pyhive", pyhive_stub)
sys.modules.setdefault("pyhive.hive", hive_stub)
yaml_stub = types.ModuleType("yaml")
yaml_stub.safe_load = lambda stream: {}
sys.modules.setdefault("yaml", yaml_stub)
loguru_stub = types.ModuleType("loguru")
loguru_stub.logger = types.SimpleNamespace(
    remove=lambda *a, **k: None,
    add=lambda *a, **k: None,
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
    exception=lambda *a, **k: None,
    debug=lambda *a, **k: None,
)
sys.modules.setdefault("loguru", loguru_stub)

from utils.load_jobs import LoadJobError
from tools.load_export import (
    build_hive_file_name,
    build_hive_sql,
    normalize_requested_dates,
    parse_describe_rows,
    parse_partition_rows,
    resolve_time_partition_field,
)


DESCRIBE_PARTITIONED = [
    ("id", "bigint", ""),
    ("name", "string", "客户名"),
    ("", "", None),
    ("# Partition Information", "", ""),
    ("# col_name", "data_type", "comment"),
    ("etl_date", "string", ""),
]

DESCRIBE_PLAIN = [
    ("branch_code", "string", ""),
    ("branch_name", "string", ""),
]

DESCRIBE_DETAILED = [
    ("id", "int", ""),
    ("# Partition Information", "", ""),
    ("# col_name", "data_type", "comment"),
    ("cdate", "string", ""),
    ("", "", None),
    ("# Detailed Table Information", "", ""),
    ("Table", "default.branch_info", ""),
]


class ParseDescribeRowsTests(unittest.TestCase):
    def test_partitioned_table_splits_data_and_partition_columns(self):
        parsed = parse_describe_rows(DESCRIBE_PARTITIONED)
        self.assertEqual([c["name"] for c in parsed["columns"]], ["id", "name"])
        self.assertEqual([c["name"] for c in parsed["partition_columns"]], ["etl_date"])

    def test_plain_table_has_no_partition_columns(self):
        parsed = parse_describe_rows(DESCRIBE_PLAIN)
        self.assertEqual(len(parsed["columns"]), 2)
        self.assertEqual(parsed["partition_columns"], [])

    def test_sections_after_partition_are_ignored(self):
        parsed = parse_describe_rows(DESCRIBE_DETAILED)
        self.assertEqual([c["name"] for c in parsed["columns"]], ["id"])
        self.assertEqual([c["name"] for c in parsed["partition_columns"]], ["cdate"])
        # Detailed Table Information 段不进入任何列
        all_names = [c["name"] for c in parsed["columns"] + parsed["partition_columns"]]
        self.assertNotIn("Table", all_names)

    def test_empty_input(self):
        parsed = parse_describe_rows([])
        self.assertEqual(parsed, {"columns": [], "partition_columns": []})


class ResolveTimePartitionFieldTests(unittest.TestCase):
    def test_etl_date_only(self):
        cols = [{"name": "etl_date"}]
        self.assertEqual(resolve_time_partition_field(cols, "etl_date"), "etl_date")

    def test_cdate_case_insensitive(self):
        cols = [{"name": "CDATE"}]
        self.assertEqual(resolve_time_partition_field(cols, "etl_date"), "CDATE")

    def test_both_present_uses_platform_primary_hint(self):
        cols = [{"name": "etl_date"}, {"name": "cdate"}]
        self.assertEqual(resolve_time_partition_field(cols, "cdate"), "cdate")
        self.assertEqual(resolve_time_partition_field(cols, "etl_date"), "etl_date")

    def test_both_present_with_invalid_hint_falls_back(self):
        cols = [{"name": "etl_date"}, {"name": "cdate"}]
        self.assertEqual(resolve_time_partition_field(cols, "biz_date"), "etl_date")
        self.assertEqual(resolve_time_partition_field(cols, ""), "etl_date")

    def test_no_time_partition_returns_none(self):
        cols = [{"name": "region"}]
        self.assertIsNone(resolve_time_partition_field(cols, "etl_date"))
        self.assertIsNone(resolve_time_partition_field([], "etl_date"))


class ParsePartitionRowsTests(unittest.TestCase):
    def test_single_level_partition(self):
        rows = [("etl_date=2026-09-11",), ("etl_date=2026-09-12",)]
        self.assertEqual(
            parse_partition_rows(rows, "etl_date"), {"2026-09-11", "2026-09-12"}
        )

    def test_multi_level_partition_takes_target_value(self):
        rows = [("etl_date=2026-09-10/region=north",), ("etl_date=2026-09-10/region=south",)]
        self.assertEqual(parse_partition_rows(rows, "etl_date"), {"2026-09-10"})

    def test_field_match_is_case_insensitive(self):
        rows = [("ETL_DATE=2026-09-11",)]
        self.assertEqual(parse_partition_rows(rows, "etl_date"), {"2026-09-11"})

    def test_empty_and_malformed_rows(self):
        self.assertEqual(parse_partition_rows([], "etl_date"), set())
        self.assertEqual(parse_partition_rows([("",), (None,), ("garbage",)], "etl_date"), set())


class BuildHiveSqlTests(unittest.TestCase):
    def test_time_partitioned_sorts_and_quotes_dates(self):
        sql = build_hive_sql("db8", "balance", "etl_date", ["2026-09-12", "2026-09-11"])
        self.assertEqual(
            sql, "SELECT * FROM `db8`.`balance` WHERE `etl_date` IN ('2026-09-11', '2026-09-12')"
        )

    def test_full_table_has_no_date_filter(self):
        sql = build_hive_sql("db8", "branch_info", None, ["2026-09-12"])
        self.assertEqual(sql, "SELECT * FROM `db8`.`branch_info`")


class BuildHiveFileNameTests(unittest.TestCase):
    def test_full_table_name_has_no_requested_date(self):
        self.assertEqual(
            build_hive_file_name("branch_info", None, []), "branch_info.parquet"
        )
        # 即使调用方传了日期，full_table 也不带日期
        self.assertEqual(
            build_hive_file_name("branch_info", None, ["2026-09-12"]), "branch_info.parquet"
        )

    def test_single_date(self):
        self.assertEqual(
            build_hive_file_name("balance", "etl_date", ["2026-09-12"]),
            "balance__2026-09-12.parquet",
        )

    def test_date_range_uses_first_and_last(self):
        self.assertEqual(
            build_hive_file_name(
                "balance", "etl_date", ["2026-09-02", "2026-09-01", "2026-09-03"]
            ),
            "balance__2026-09-01__2026-09-03.parquet",
        )


class NormalizeRequestedDatesTests(unittest.TestCase):
    def test_dedupes_and_sorts(self):
        self.assertEqual(
            normalize_requested_dates(["2026-09-12", "2026-09-11", "2026-09-12"]),
            ["2026-09-11", "2026-09-12"],
        )

    def test_empty(self):
        self.assertEqual(normalize_requested_dates([]), [])
        self.assertEqual(normalize_requested_dates(None), [])

    def test_rejects_bad_format(self):
        for bad in ("2026-9-1", "20260912", "2026/09/12", "", "2026-09-12 ", 20260912):
            with self.subTest(bad=bad):
                with self.assertRaises(LoadJobError):
                    normalize_requested_dates([bad])

    def test_rejects_impossible_calendar_date(self):
        with self.assertRaises(LoadJobError):
            normalize_requested_dates(["2026-02-30"])

    def test_rejects_too_many_dates(self):
        with self.assertRaises(LoadJobError):
            normalize_requested_dates([f"2026-01-{d:02d}" for d in range(1, 32)] * 20)


if __name__ == "__main__":
    unittest.main()
