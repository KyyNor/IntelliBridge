"""source_version 解析测试（issue #3）。

夹具是**真实 Spark 3.1.3 ThriftServer 的 DESCRIBE FORMATTED 输出**（逐行照抄），
外加 Hive 风格的段头形状（HiveServer2 把参数逐行列出）——两种形状都要能解析。
"""

import sys
import types
import unittest

# 轻量环境兼容桩（生产环境由 requirements.txt 提供）
for name in ("pyhive",):
    sys.modules.setdefault(name, types.ModuleType(name))
hive_stub = types.ModuleType("pyhive.hive")
hive_stub.Connection = object
sys.modules.setdefault("pyhive.hive", hive_stub)

from utils.source_version import (  # noqa: E402
    extract_source_version,
    normalize_version,
    parse_describe_formatted,
    parse_inline_map,
)

# --- 真实输出：DESCRIBE FORMATTED `db8`.`balance` PARTITION (`etl_date`='2026-09-11')
REAL_SPARK_PARTITION_ROWS = [
    ("id", "bigint", None),
    ("cust_name", "string", None),
    ("amount", "decimal(10,2)", None),
    ("txn_time", "timestamp", None),
    ("etl_date", "string", None),
    ("# Partition Information", "", ""),
    ("# col_name", "data_type", "comment"),
    ("etl_date", "string", None),
    ("", "", ""),
    ("# Detailed Partition Information", "", ""),
    ("Database", "db8", ""),
    ("Table", "balance", ""),
    ("Partition Values", "[etl_date=2026-09-11]", ""),
    ("Location", "file:/tmp/spark-warehouse/db8.db/balance/etl_date=2026-09-11", ""),
    ("Serde Library", "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe", ""),
    ("InputFormat", "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat", ""),
    ("OutputFormat", "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat", ""),
    (
        "Storage Properties",
        "[serialization.format=1, path=file:/tmp/spark-warehouse/db8.db/balance]",
        "",
    ),
    (
        "Partition Parameters",
        "{transient_lastDdlTime=1789617938, totalSize=2219, numFiles=2}",
        "",
    ),
    ("Created Time", "Thu Sep 17 04:05:38 UTC 2026", ""),
    ("Last Access", "UNKNOWN", ""),
    ("Partition Statistics", "2219 bytes", ""),
    ("", "", ""),
    ("# Storage Information", "", ""),
    ("Location", "file:/tmp/spark-warehouse/db8.db/balance", ""),
    ("Serde Library", "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe", ""),
]

# --- 真实输出：DESCRIBE FORMATTED `db8`.`with_props`（Hive-serde 表带表属性）
REAL_SPARK_TABLE_ROWS = [
    ("id", "bigint", None),
    ("name", "string", None),
    ("", "", ""),
    ("# Detailed Table Information", "", ""),
    ("Database", "db8", ""),
    ("Table", "with_props", ""),
    ("Owner", "root", ""),
    ("Created Time", "Thu Sep 17 04:49:19 UTC 2026", ""),
    ("Last Access", "UNKNOWN", ""),
    ("Created By", "Spark 3.1.3", ""),
    ("Type", "MANAGED", ""),
    ("Provider", "hive", ""),
    ("Table Properties", "[transient_lastDdlTime=1789620559]", ""),
    ("Location", "file:/tmp/spark-warehouse/db8.db/with_props", ""),
    ("Serde Library", "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe", ""),
    ("Storage Properties", "[serialization.format=1]", ""),
    ("Partition Provider", "Catalog", ""),
]

# --- 真实输出：无表属性的 Spark 管理表（dim.branch_info，读不到版本）
REAL_SPARK_TABLE_NO_PROPS_ROWS = [
    ("branch_code", "string", None),
    ("branch_name", "string", None),
    ("opened", "date", None),
    ("", "", ""),
    ("# Detailed Table Information", "", ""),
    ("Database", "dim", ""),
    ("Table", "branch_info", ""),
    ("Created Time", "Thu Sep 17 04:05:38 UTC 2026", ""),
    ("Type", "MANAGED", ""),
    ("Provider", "parquet", ""),
    ("Location", "file:/tmp/spark-warehouse/dim.db/branch_info", ""),
]

# --- Hive 风格（HiveServer2 逐行列出参数；本仓库测试夹具，非实测采样）
HIVE_STYLE_PARTITION_ROWS = [
    ("# Detailed Partition Information", "", ""),
    ("Partition Values", "[etl_date=2026-09-11]", ""),
    ("Partition Parameters:", "", ""),
    ("transient_lastDdlTime", "1789529400", ""),
    ("numFiles", "1", ""),
    ("", "", ""),
    ("Location", "hdfs://nn/db8.db/balance/etl_date=2026-09-11", ""),
]

HIVE_STYLE_TABLE_ROWS = [
    ("# Detailed Table Information", "", ""),
    ("Database", "db8", ""),
    ("Table Parameters:", "", ""),
    ("transient_lastDdlTime", "1789615800", ""),
    ("numFiles", "2", ""),
    ("", "", ""),
    ("Location", "hdfs://nn/db8.db/balance", ""),
]


class ParseInlineMapTests(unittest.TestCase):
    def test_brace_and_bracket_maps(self):
        self.assertEqual(
            parse_inline_map("{transient_lastDdlTime=1789617938, numFiles=2}"),
            {"transient_lastDdlTime": "1789617938", "numFiles": "2"},
        )
        self.assertEqual(
            parse_inline_map("[serialization.format=1]"), {"serialization.format": "1"}
        )

    def test_values_with_separators_are_not_split(self):
        parsed = parse_inline_map("[serialization.format=1, path=file:/tmp/a.db]")
        # 值里的 `:` `/` 必须原样保留
        self.assertEqual(parsed["path"], "file:/tmp/a.db")

    def test_non_map_input_returns_empty(self):
        for text in ("", None, "UNKNOWN", "2 files", "[]", "{}"):
            self.assertEqual(parse_inline_map(text), {}, text)


class ParseDescribeFormattedTests(unittest.TestCase):
    def test_spark_partition_params_are_isolated(self):
        sections = parse_describe_formatted(REAL_SPARK_PARTITION_ROWS)
        self.assertEqual(
            sections["partition_parameters"]["transient_lastDdlTime"], "1789617938"
        )
        # 内联 map 之后的行（Created Time/Partition Statistics 等）不属于参数段
        self.assertNotIn("Created Time", sections["partition_parameters"])
        self.assertNotIn("Partition Statistics", sections["partition_parameters"])

    def test_spark_table_properties(self):
        sections = parse_describe_formatted(REAL_SPARK_TABLE_ROWS)
        self.assertEqual(sections["table_properties"]["transient_lastDdlTime"], "1789620559")
        # Provider=Type 等普通行不能被当成参数
        self.assertNotIn("Provider", sections.get("table_properties", {}))

    def test_hive_style_section_rows(self):
        sections = parse_describe_formatted(HIVE_STYLE_PARTITION_ROWS)
        self.assertEqual(sections["partition_parameters"]["transient_lastDdlTime"], "1789529400")
        # 空行结束段落：后面的 Location 不能混进来
        self.assertNotIn("Location", sections["partition_parameters"])

        sections = parse_describe_formatted(HIVE_STYLE_TABLE_ROWS)
        self.assertEqual(sections["table_parameters"]["transient_lastDdlTime"], "1789615800")

    def test_empty_and_garbage_rows(self):
        self.assertEqual(parse_describe_formatted([]), {})
        self.assertEqual(parse_describe_formatted(None), {})
        self.assertEqual(parse_describe_formatted([(), ("",)]) , {})


class ExtractSourceVersionTests(unittest.TestCase):
    def test_partition_version_from_spark_output(self):
        self.assertEqual(
            extract_source_version(REAL_SPARK_PARTITION_ROWS, "partition"),
            ("1789617938", "partition_parameters"),
        )

    def test_table_version_from_spark_output(self):
        self.assertEqual(
            extract_source_version(REAL_SPARK_TABLE_ROWS, "table"),
            ("1789620559", "table_properties"),
        )

    def test_table_properties_never_used_as_partition_version(self):
        """表级属性对所有分区相同——拿它当分区版本会让所有分区一起“没变”。"""
        self.assertEqual(extract_source_version(REAL_SPARK_TABLE_ROWS, "partition"), (None, None))

    def test_partition_params_not_used_as_table_version(self):
        self.assertEqual(
            extract_source_version(REAL_SPARK_PARTITION_ROWS, "table"), (None, None)
        )

    def test_table_without_properties_has_no_version(self):
        """实测：Spark 管理表可能根本没有表属性 → 必须返回 None 而不是编一个。"""
        self.assertEqual(extract_source_version(REAL_SPARK_TABLE_NO_PROPS_ROWS, "table"), (None, None))

    def test_hive_style_shapes(self):
        self.assertEqual(
            extract_source_version(HIVE_STYLE_PARTITION_ROWS, "partition"),
            ("1789529400", "partition_parameters"),
        )
        self.assertEqual(
            extract_source_version(HIVE_STYLE_TABLE_ROWS, "table"),
            ("1789615800", "table_parameters"),
        )

    def test_missing_or_blank_version_is_none(self):
        rows = [
            ("# Detailed Partition Information", "", ""),
            ("Partition Parameters", "{totalSize=2219, numFiles=2}", ""),
        ]
        self.assertEqual(extract_source_version(rows, "partition"), (None, None))
        rows = [
            ("Partition Parameters", "{transient_lastDdlTime=  , numFiles=2}", ""),
        ]
        self.assertEqual(extract_source_version(rows, "partition"), (None, None))

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_source_version(REAL_SPARK_PARTITION_ROWS, "database")

    def test_normalize_version(self):
        self.assertEqual(normalize_version(1789617938), "1789617938")
        self.assertEqual(normalize_version(" 1789617938 "), "1789617938")
        self.assertIsNone(normalize_version(""))
        self.assertIsNone(normalize_version("   "))
        self.assertIsNone(normalize_version(None))


if __name__ == "__main__":
    unittest.main()
