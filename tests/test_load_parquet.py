import datetime
import decimal
import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

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

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    PYARROW_OK = True
except ImportError:
    PYARROW_OK = False

try:
    import duckdb
    DUCKDB_OK = True
except ImportError:
    DUCKDB_OK = False

from utils.parquet_export import (
    ParquetExportError,
    convert_value,
    file_digest,
    hive_arrow_type,
    infer_arrow_type,
    mysql_arrow_type,
    stream_to_parquet,
)


class _BatchCursor:
    """fetchmany 语义的假游标：记录调用并分批吐出行。"""

    def __init__(self, rows, names, batch_hint=None):
        self.rows = list(rows)
        self.names = list(names)
        self.description = [(n, None, None, None, None, None, None) for n in names]
        self.calls = []

    def fetchmany(self, size):
        self.calls.append(size)
        batch, self.rows = self.rows[:size], self.rows[size:]
        return batch


@unittest.skipUnless(PYARROW_OK, "pyarrow 未安装")
class ArrowTypeMappingTests(unittest.TestCase):
    def test_hive_types(self):
        self.assertEqual(hive_arrow_type("string"), pa.string())
        self.assertEqual(hive_arrow_type("BIGINT"), pa.int64())
        self.assertEqual(hive_arrow_type("decimal(10,2)"), pa.decimal128(10, 2))
        self.assertEqual(hive_arrow_type("date"), pa.date32())
        self.assertEqual(hive_arrow_type("timestamp"), pa.timestamp("us"))
        self.assertEqual(hive_arrow_type("array<int>"), pa.string())  # 未识别退化为 string

    def test_mysql_types(self):
        self.assertEqual(mysql_arrow_type("int"), pa.int32())
        self.assertEqual(mysql_arrow_type("int unsigned"), pa.int64())
        self.assertEqual(mysql_arrow_type("bigint unsigned"), pa.uint64())
        self.assertEqual(mysql_arrow_type("tinyint(1)"), pa.int8())
        self.assertEqual(mysql_arrow_type("decimal(12,4)"), pa.decimal128(12, 4))
        self.assertEqual(mysql_arrow_type("datetime"), pa.timestamp("us"))
        self.assertEqual(mysql_arrow_type("bit(1)"), pa.binary())
        self.assertEqual(mysql_arrow_type("varbinary(32)"), pa.binary())
        self.assertEqual(mysql_arrow_type("text"), pa.string())
        self.assertEqual(mysql_arrow_type("json"), pa.string())

    def test_convert_value_normalizes_driver差异(self):
        # Spark Thrift 常把 date/timestamp 返回为字符串
        self.assertEqual(convert_value("2026-09-12", pa.date32()), datetime.date(2026, 9, 12))
        self.assertEqual(
            convert_value("2026-09-12 10:30:00", pa.timestamp("us")),
            datetime.datetime(2026, 9, 12, 10, 30),
        )
        self.assertEqual(convert_value(datetime.datetime(2026, 9, 12), pa.date32()), datetime.date(2026, 9, 12))
        self.assertEqual(convert_value(b"hello", pa.string()), "hello")
        self.assertIs(convert_value(None, pa.int64()), None)
        self.assertEqual(convert_value("123", pa.int64()), 123)
        self.assertEqual(convert_value(decimal.Decimal("1.5"), pa.string()), "1.5")
        self.assertTrue(convert_value("true", pa.bool_()))
        with self.assertRaises(ValueError):
            convert_value("abc", pa.int64())

    def test_infer_arrow_type(self):
        self.assertEqual(infer_arrow_type([None, 3]), pa.int64())
        self.assertEqual(infer_arrow_type([None, 1.5]), pa.float64())
        self.assertEqual(infer_arrow_type([None, True]), pa.bool_())
        self.assertEqual(infer_arrow_type([decimal.Decimal("0.000001")]), pa.decimal128(6, 6))
        self.assertEqual(infer_arrow_type([None, None]), pa.string())


@unittest.skipUnless(PYARROW_OK, "pyarrow 未安装")
class StreamToParquetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out.parquet"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_preserves_types_and_nulls(self):
        rows = [
            (1, "a", decimal.Decimal("12.34"), datetime.date(2026, 9, 12), datetime.datetime(2026, 9, 12, 1, 2, 3), 1.5, True, b"k"),
            (None, None, None, None, None, None, None, None),
            (2, "b", decimal.Decimal("-0.01"), datetime.date(2024, 2, 29), None, -2.25, False, b""),
        ]
        names = ["id", "name", "amount", "d", "ts", "score", "flag", "payload"]
        declared = ["bigint", "string", "decimal(10,2)", "date", "timestamp", "double", "boolean", "binary"]
        cursor = _BatchCursor(rows, names)
        count, resolved = stream_to_parquet(
            cursor.fetchmany,
            list(zip(names, declared)),
            self.out,
            batch_rows=2,
            declared_to_arrow=hive_arrow_type,
        )
        self.assertEqual(count, 3)
        self.assertEqual([n for n, _ in resolved], names)

        table = pq.read_table(self.out)
        self.assertEqual(table.num_rows, 3)
        self.assertEqual(table.column("id").to_pylist(), [1, None, 2])
        self.assertEqual(table.column("name").to_pylist(), ["a", None, "b"])
        self.assertEqual(table.column("amount").to_pylist(), [decimal.Decimal("12.34"), None, decimal.Decimal("-0.01")])
        self.assertEqual(table.column("d").to_pylist()[2], datetime.date(2024, 2, 29))
        self.assertIsNone(table.column("ts").to_pylist()[2])
        self.assertEqual(table.column("flag").to_pylist(), [True, None, False])
        self.assertEqual(table.column("payload").to_pylist(), [b"k", None, b""])

    def test_chunked_fetchmany_is_used(self):
        rows = [(i, f"n{i}") for i in range(10)]
        cursor = _BatchCursor(rows, ["id", "name"])
        stream_to_parquet(
            cursor.fetchmany,
            [("id", "int"), ("name", "string")],
            self.out,
            batch_rows=4,
            declared_to_arrow=hive_arrow_type,
        )
        # 10 行按 4 行一批 → 至少 3 次 fetch，且每次都带 batch 大小
        self.assertEqual(cursor.calls, [4, 4, 4, 4])
        self.assertEqual(pq.read_table(self.out).num_rows, 10)

    def test_zero_rows_produces_valid_schema_only_parquet(self):
        names = ["id", "name"]
        cursor = _BatchCursor([], names)
        count, resolved = stream_to_parquet(
            cursor.fetchmany,
            [("id", "bigint"), ("name", "string")],
            self.out,
            declared_to_arrow=hive_arrow_type,
        )
        self.assertEqual(count, 0)
        self.assertEqual([n for n, _ in resolved], names)
        table = pq.read_table(self.out)
        self.assertEqual(table.num_rows, 0)
        self.assertEqual(table.column_names, names)

    def test_should_stop_aborts_between_batches(self):
        class StopNow(Exception):
            pass

        rows = [(1,), (2,), (3,)]
        cursor = _BatchCursor(rows, ["id"])
        with self.assertRaises(StopNow):
            stream_to_parquet(
                cursor.fetchmany,
                [("id", "bigint")],
                self.out,
                batch_rows=1,
                should_stop=lambda: (_ for _ in ()).throw(StopNow()) if cursor.calls else None,
                declared_to_arrow=hive_arrow_type,
            )
        self.assertEqual(len(cursor.calls), 1)  # 第一批之后即停止

    def test_conversion_error_carries_column_context(self):
        rows = [("not-a-number",)]
        cursor = _BatchCursor(rows, ["v"])
        with self.assertRaises(ParquetExportError) as ctx:
            stream_to_parquet(
                cursor.fetchmany,
                [("v", "bigint")],
                self.out,
                declared_to_arrow=hive_arrow_type,
            )
        self.assertIn("v", str(ctx.exception))

    def test_file_digest(self):
        payload = b"0123456789" * 1000
        self.out.write_bytes(payload)
        size, sha = file_digest(self.out)
        self.assertEqual(size, len(payload))
        self.assertEqual(sha, hashlib.sha256(payload).hexdigest())

    @unittest.skipUnless(DUCKDB_OK, "duckdb 未安装（测试专用依赖）")
    def test_duckdb_can_read_exported_parquet(self):
        rows = [
            (i, f"name-{i}", decimal.Decimal(f"{i}.25"), datetime.date(2026, 9, (i % 28) + 1), i * 1.5)
            for i in range(100)
        ]
        names = ["id", "name", "amount", "d", "score"]
        declared = ["bigint", "string", "decimal(10,2)", "date", "double"]
        cursor = _BatchCursor(rows, names)
        stream_to_parquet(
            cursor.fetchmany, list(zip(names, declared)), self.out,
            batch_rows=30, declared_to_arrow=hive_arrow_type,
        )
        rel = duckdb.sql(f"SELECT count(*) AS n, sum(id) AS s, sum(amount) AS a FROM '{self.out}'")
        result = rel.fetchall()
        self.assertEqual(result[0][0], 100)
        self.assertEqual(result[0][1], sum(r[0] for r in rows))
        self.assertEqual(result[0][2], decimal.Decimal(str(sum(float(r[2]) for r in rows))))
        types = duckdb.sql(
            f"DESCRIBE SELECT * FROM '{self.out}'"
        ).fetchall()
        type_names = {row[0]: row[1] for row in types}
        self.assertEqual(type_names["id"], "BIGINT")
        self.assertEqual(type_names["amount"], "DECIMAL(10,2)")
        self.assertEqual(type_names["d"], "DATE")
        self.assertEqual(type_names["score"], "DOUBLE")


if __name__ == "__main__":
    unittest.main()
