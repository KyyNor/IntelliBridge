"""Conditional load 测试（issue #3）：known version 相同 → 不扫描数据、不生成 Parquet。

夹具的 DESCRIBE FORMATTED 行照抄真实 Spark 3.1.3 ThriftServer 输出形状，
每条 SQL 都必须显式脚本化——未脚本化的语句直接断言失败，
这样「not_modified 路径绝不执行 SELECT」这类要求才真的被验证。
"""

import re
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path

# 轻量环境兼容桩（生产环境由 requirements.txt 提供）
pyhive_stub = types.ModuleType("pyhive")
hive_stub = types.ModuleType("pyhive.hive")
hive_stub.Connection = object
pyhive_stub.hive = hive_stub
sys.modules.setdefault("pyhive", pyhive_stub)
sys.modules.setdefault("pyhive.hive", hive_stub)

from tools.load_export import (  # noqa: E402
    LoadExportService,
    build_partition_describe_sql,
    build_version_check,
    normalize_known_version,
    normalize_known_versions,
    resolve_version_status,
)
from utils.load_jobs import LoadJobError, LoadJobManager  # noqa: E402

DATE_IN_SQL = re.compile(r"'(\d{4}-\d{2}-\d{2})'")

HIVE_DESCRIBE_ETL = {
    "columns": [
        {"name": "id", "type": "bigint", "comment": ""},
        {"name": "cust_name", "type": "string", "comment": ""},
    ],
    "partition_columns": [{"name": "etl_date", "type": "string", "comment": ""}],
}

HIVE_DESCRIBE_CDATE = {
    "columns": [{"name": "id", "type": "bigint", "comment": ""}],
    "partition_columns": [{"name": "CDATE", "type": "string", "comment": ""}],
}

HIVE_DESCRIBE_FULL = {
    "columns": [
        {"name": "branch_code", "type": "string", "comment": ""},
        {"name": "branch_name", "type": "string", "comment": ""},
    ],
    "partition_columns": [],
}


def partition_rows(version, *, total_size=2219, num_files=2):
    """分区级 DESCRIBE FORMATTED 输出（真实形状）；version=None 表示读不到版本。"""
    params = f"{{totalSize={total_size}, numFiles={num_files}}}"
    if version:
        params = f"{{transient_lastDdlTime={version}, totalSize={total_size}, numFiles={num_files}}}"
    return [
        ("id", "bigint", None),
        ("cust_name", "string", None),
        ("# Partition Information", "", ""),
        ("# col_name", "data_type", "comment"),
        ("etl_date", "string", None),
        ("", "", ""),
        ("# Detailed Partition Information", "", ""),
        ("Database", "db8", ""),
        ("Table", "balance", ""),
        ("Partition Parameters", params, ""),
        ("", "", ""),
        ("# Storage Information", "", ""),
        ("Location", "file:/tmp/spark-warehouse/db8.db/balance/etl_date=2026-09-11", ""),
    ]


def table_rows(version):
    """表级 DESCRIBE FORMATTED 输出；version=None 表示表没有属性（实测存在）。"""
    rows = [
        ("id", "bigint", None),
        ("cust_name", "string", None),
        ("", "", ""),
        ("# Detailed Table Information", "", ""),
        ("Database", "db8", ""),
        ("Table", "balance", ""),
        ("Provider", "hive", ""),
    ]
    if version:
        rows.append(("Table Properties", f"[transient_lastDdlTime={version}]", ""))
    rows.append(("Location", "file:/tmp/spark-warehouse/db8.db/balance", ""))
    return rows


class _HiveCursor:
    """脚本化 Hive 游标：按语句类型返回夹具，并记录每类语句的调用。"""

    def __init__(
        self,
        *,
        describe_rows,
        partitions=(),
        versions=None,
        table_version=None,
        rows_by_date=None,
        select_names,
        partition_field="etl_date",
        fail_versions=False,
    ):
        self.describe_rows = describe_rows
        self.partitions = list(partitions)
        self.partition_field = partition_field
        self.versions = dict(versions or {})
        self.table_version = table_version
        self.rows_by_date = dict(rows_by_date or {})
        self.select_names = list(select_names)
        self.fail_versions = fail_versions
        self.executed = []
        self.version_queries = []
        self.select_queries = []
        self._pending = []

    def execute(self, sql):
        self.executed.append(sql)
        self._pending = []
        if sql.startswith("DESCRIBE FORMATTED"):
            if self.fail_versions:
                raise RuntimeError("元数据会话不可用")
            match = DATE_IN_SQL.search(sql)
            if match:
                date = match.group(1)
                self.version_queries.append(date)
                self._pending = partition_rows(self.versions.get(date))
            else:
                self.version_queries.append(None)
                self._pending = table_rows(self.table_version)
            return
        if sql.startswith("DESCRIBE "):
            self._pending = self.describe_rows
            return
        if sql.startswith("SHOW PARTITIONS"):
            self._pending = [(f"{self.partition_field}={d}",) for d in self.partitions]
            return
        if sql.startswith("SELECT"):
            date = DATE_IN_SQL.search(sql).group(1) if DATE_IN_SQL.search(sql) else None
            self.select_queries.append(date)
            self._pending = self.rows_by_date.get(date, [])
            return
        raise AssertionError(f"未脚本化的 SQL: {sql}")

    def fetchall(self):
        return self._pending

    def fetchmany(self, size):
        batch, self._pending = self._pending[:size], self._pending[size:]
        return batch

    @property
    def description(self):
        return [(n, None, None, None, None, None, None) for n in self.select_names]

    def close(self):
        pass


class _FakeHiveConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _wait_view(manager, job_id, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = manager.get_view(job_id)
        if view is not None and view["status"] in ("ready", "failed"):
            return view
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内进入终态")


class _ConditionalTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = LoadJobManager(
            Path(self.tmp.name) / "spool", ttl_seconds=3600, sweep_interval_seconds=9999
        )

    def tearDown(self):
        self.manager.stop()
        self.tmp.cleanup()

    def _service(self, cursor, describe):
        return LoadExportService(
            self.manager,
            hive_connection=lambda timeout=None: _conn_ctx(cursor),
            hive_describe=lambda db, tbl: dict(describe),
        )


@contextmanager
def _conn_ctx(cursor):
    yield _FakeHiveConn(cursor)


class TimePartitionedConditionalTests(_ConditionalTestCase):
    def _run(self, *, versions, known_versions, dates, rows_by_date, partitions=None):
        cursor = _HiveCursor(
            describe_rows=HIVE_DESCRIBE_ETL,
            partitions=partitions or dates,
            versions=versions,
            rows_by_date=rows_by_date,
            select_names=["id", "cust_name"],
        )
        service = self._service(cursor, HIVE_DESCRIBE_ETL)
        submitted = service.submit_hive(
            "db8", "balance", dates, known_versions=known_versions
        )
        self.assertIsNone(submitted.get("error"))
        return cursor, _wait_view(self.manager, submitted["job_id"])

    def test_not_modified_partition_skips_select_and_parquet(self):
        cursor, view = self._run(
            versions={"2026-09-11": "100"},
            known_versions={"2026-09-11": "100"},
            dates=["2026-09-11"],
            rows_by_date={"2026-09-11": [(1, "alice")]},
        )
        self.assertEqual(view["status"], "ready", view.get("error"))
        # 版本被比较过，但没有任何数据扫描、没有文件
        self.assertEqual(cursor.version_queries, ["2026-09-11"])
        self.assertEqual(cursor.select_queries, [])
        self.assertEqual(view["files"], [])
        self.assertEqual(view["byte_size"], 0)
        self.assertEqual(view["row_count"], 0)
        self.assertEqual(view["actual_dates"], [])
        self.assertEqual(
            view["partitions"],
            [
                {
                    "logical_date": "2026-09-11",
                    "status": "not_modified",
                    "source_version": "100",
                    "file_id": None,
                }
            ],
        )
        self.assertEqual(
            view["version_check"],
            {"requested": True, "not_modified": 1, "modified": 0, "unknown": 0},
        )

    def test_changed_version_exports_new_parquet(self):
        import pyarrow.parquet as pq

        cursor, view = self._run(
            versions={"2026-09-11": "200"},
            known_versions={"2026-09-11": "100"},
            dates=["2026-09-11"],
            rows_by_date={"2026-09-11": [(1, "alice"), (2, "bob")]},
        )
        self.assertEqual(view["status"], "ready", view.get("error"))
        self.assertEqual(cursor.select_queries, ["2026-09-11"])
        self.assertEqual(view["row_count"], 2)
        self.assertEqual(view["actual_dates"], ["2026-09-11"])
        self.assertEqual(view["partitions"][0]["status"], "modified")
        self.assertEqual(view["partitions"][0]["source_version"], "200")
        self.assertEqual(view["partitions"][0]["file_id"], "file_1")
        self.assertEqual([f["name"] for f in view["files"]], ["balance__2026-09-11.parquet"])
        table = pq.read_table(self.manager.open_file(view["job_id"], "file_1")[0])
        self.assertEqual(table.column("id").to_pylist(), [1, 2])
        self.assertEqual(
            view["version_check"],
            {"requested": True, "not_modified": 0, "modified": 1, "unknown": 0},
        )

    def test_range_mixed_not_modified_and_modified(self):
        cursor, view = self._run(
            versions={"2026-09-11": "100", "2026-09-12": "999"},
            known_versions={"2026-09-11": "100", "2026-09-12": "500"},
            dates=["2026-09-11", "2026-09-12"],
            rows_by_date={
                "2026-09-11": [(1, "alice")],
                "2026-09-12": [(2, "bob"), (3, None)],
            },
        )
        self.assertEqual(view["status"], "ready", view.get("error"))
        # 只对变化的日期执行 SELECT，文件也只有它一个
        self.assertEqual(cursor.select_queries, ["2026-09-12"])
        self.assertEqual(cursor.version_queries, ["2026-09-11", "2026-09-12"])
        self.assertEqual([f["name"] for f in view["files"]], ["balance__2026-09-12.parquet"])
        self.assertEqual([f["logical_date"] for f in view["files"]], ["2026-09-12"])
        self.assertEqual(view["actual_dates"], ["2026-09-12"])
        self.assertEqual(view["row_count"], 2)
        self.assertEqual(
            [(p["logical_date"], p["status"], p["file_id"]) for p in view["partitions"]],
            [("2026-09-11", "not_modified", None), ("2026-09-12", "modified", "file_1")],
        )
        self.assertEqual(
            view["version_check"],
            {"requested": True, "not_modified": 1, "modified": 1, "unknown": 0},
        )

    def test_without_known_versions_keeps_plain_load_behaviour(self):
        cursor, view = self._run(
            versions={"2026-09-11": "100", "2026-09-12": "200"},
            known_versions=None,
            dates=["2026-09-11", "2026-09-12"],
            rows_by_date={"2026-09-11": [(1, "a")], "2026-09-12": [(2, "b")]},
        )
        self.assertEqual(view["status"], "ready", view.get("error"))
        # 未要求比较：全部导出（与 #1 行为一致），但当前版本仍然回传供调用方建缓存
        self.assertEqual(cursor.select_queries, ["2026-09-11", "2026-09-12"])
        self.assertEqual(view["actual_dates"], ["2026-09-11", "2026-09-12"])
        self.assertEqual([p["status"] for p in view["partitions"]], ["modified", "modified"])
        self.assertEqual([p["source_version"] for p in view["partitions"]], ["100", "200"])
        self.assertEqual(
            view["version_check"],
            {"requested": False, "not_modified": 0, "modified": 2, "unknown": 0},
        )

    def test_unreadable_version_is_unknown_but_still_exports(self):
        """读不到版本绝不能被当成 not_modified（否则调用方会一直用陈旧数据）。"""
        cursor, view = self._run(
            versions={"2026-09-11": None},
            known_versions={"2026-09-11": "100"},
            dates=["2026-09-11"],
            rows_by_date={"2026-09-11": [(1, "alice")]},
        )
        self.assertEqual(view["status"], "ready", view.get("error"))
        self.assertEqual(cursor.select_queries, ["2026-09-11"])
        self.assertEqual(view["partitions"][0]["status"], "unknown")
        self.assertIsNone(view["partitions"][0]["source_version"])
        self.assertEqual(len(view["files"]), 1)
        self.assertEqual(
            view["version_check"],
            {"requested": True, "not_modified": 0, "modified": 0, "unknown": 1},
        )

    def test_version_probe_failure_degrades_to_plain_load(self):
        cursor = _HiveCursor(
            describe_rows=HIVE_DESCRIBE_ETL,
            partitions=["2026-09-11"],
            rows_by_date={"2026-09-11": [(1, "alice")]},
            select_names=["id", "cust_name"],
            fail_versions=True,
        )
        service = self._service(cursor, HIVE_DESCRIBE_ETL)
        submitted = service.submit_hive(
            "db8", "balance", ["2026-09-11"], known_versions={"2026-09-11": "100"}
        )
        view = _wait_view(self.manager, submitted["job_id"])
        # 元数据读失败不能让整个 load 失败：退化为普通导出，但状态必须是 unknown
        self.assertEqual(view["status"], "ready", view.get("error"))
        self.assertEqual(cursor.select_queries, ["2026-09-11"])
        self.assertEqual(len(view["files"]), 1)
        self.assertEqual(view["partitions"][0]["status"], "unknown")

    def test_physical_field_never_leaks_into_partition_identity(self):
        """cdate 场景：分区身份只出现 logical_date，不出现物理字段名。"""
        rows = {
            "2026-09-12": [(1, "2026-09-12")],
        }
        cursor = _HiveCursor(
            describe_rows=HIVE_DESCRIBE_CDATE,
            partitions=["2026-09-12"],
            versions={"2026-09-12": "555"},
            rows_by_date=rows,
            select_names=["id", "CDATE"],
            partition_field="cdate",
        )
        service = self._service(cursor, HIVE_DESCRIBE_CDATE)
        service._primary_time_partition = lambda: "cdate"
        submitted = service.submit_hive("db8", "balance", ["2026-09-12"])
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "ready", view.get("error"))
        self.assertEqual(view["files"][0]["logical_date"], "2026-09-12")
        self.assertEqual(
            set(view["partitions"][0]),
            {"logical_date", "status", "source_version", "file_id"},
        )
        self.assertNotIn("CDATE", repr(view["partitions"]))
        # 物理字段只出现在 IB 内部的探测 SQL 里，响应中只有 logical_date
        probe = [s for s in cursor.version_queries]
        self.assertEqual(probe, ["2026-09-12"])
        self.assertTrue(
            any("`CDATE`" in s for s in cursor.executed if s.startswith("DESCRIBE FORMATTED"))
        )

    def test_missing_partition_fails_before_version_probe(self):
        cursor = _HiveCursor(
            describe_rows=HIVE_DESCRIBE_ETL,
            partitions=["2026-09-11"],
            versions={"2026-09-13": "1"},
            select_names=["id", "cust_name"],
        )
        service = self._service(cursor, HIVE_DESCRIBE_ETL)
        submitted = service.submit_hive(
            "db8", "balance", ["2026-09-13"], known_versions={"2026-09-13": "1"}
        )
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "failed")
        self.assertIn("2026-09-13", view["error"])
        # 分区存在性先于版本读取：不存在的分区不去读版本、更不导出
        self.assertEqual(cursor.version_queries, [])
        self.assertEqual(cursor.select_queries, [])
        self.assertEqual(view["files"], [])

    def test_time_partitioned_rejects_table_level_known_version(self):
        cursor = _HiveCursor(
            describe_rows=HIVE_DESCRIBE_ETL,
            partitions=["2026-09-11"],
            select_names=["id", "cust_name"],
        )
        service = self._service(cursor, HIVE_DESCRIBE_ETL)
        submitted = service.submit_hive(
            "db8", "balance", ["2026-09-11"], known_version="1789615800"
        )
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "failed")
        self.assertIn("known_versions", view["error"])
        self.assertEqual(cursor.select_queries, [])


class FullTableConditionalTests(_ConditionalTestCase):
    def _run(self, *, table_version, known_version, rows):
        cursor = _HiveCursor(
            describe_rows=HIVE_DESCRIBE_FULL,
            table_version=table_version,
            rows_by_date={None: rows},
            select_names=["branch_code", "branch_name"],
        )
        service = self._service(cursor, HIVE_DESCRIBE_FULL)
        submitted = service.submit_hive("dim", "branch_info", [], known_version=known_version)
        self.assertIsNone(submitted.get("error"))
        return cursor, _wait_view(self.manager, submitted["job_id"])

    def test_unchanged_table_version_skips_export(self):
        cursor, view = self._run(
            table_version="1789620559",
            known_version="1789620559",
            rows=[("B001", "武汉")],
        )
        self.assertEqual(view["status"], "ready", view.get("error"))
        self.assertEqual([s for s in cursor.executed if s.startswith("SELECT")], [])
        self.assertEqual(view["files"], [])
        self.assertEqual(view["load_mode"], "full_table")
        self.assertEqual(view["source_version"], "1789620559")
        self.assertEqual(view["version_source"], "table_properties")
        self.assertEqual(view["version_status"], "not_modified")
        self.assertEqual(view["actual_dates"], [])
        self.assertEqual(view["partitions"], [])
        self.assertEqual(
            view["version_check"],
            {"requested": True, "not_modified": 1, "modified": 0, "unknown": 0},
        )

    def test_changed_table_version_exports(self):
        cursor, view = self._run(
            table_version="1789620999",
            known_version="1789620559",
            rows=[("B001", "武汉")],
        )
        self.assertEqual(view["status"], "ready", view.get("error"))
        self.assertEqual(view["version_status"], "modified")
        self.assertEqual(view["source_version"], "1789620999")
        self.assertEqual([f["name"] for f in view["files"]], ["branch_info.parquet"])
        self.assertEqual(view["row_count"], 1)
        self.assertEqual(view["actual_dates"], [])  # full_table 没有逻辑日期语义

    def test_table_without_version_degrades_to_unknown(self):
        cursor, view = self._run(
            table_version=None, known_version="1789620559", rows=[("B001", "武汉")]
        )
        self.assertEqual(view["status"], "ready", view.get("error"))
        self.assertEqual(view["version_status"], "unknown")
        self.assertIsNone(view["source_version"])
        self.assertIsNone(view["version_source"])
        self.assertEqual(len(view["files"]), 1)

    def test_no_known_version_reports_current_version(self):
        cursor, view = self._run(
            table_version="1789620559", known_version=None, rows=[("B001", "武汉")]
        )
        self.assertEqual(view["status"], "ready", view.get("error"))
        self.assertEqual(view["version_status"], "modified")
        self.assertEqual(view["source_version"], "1789620559")
        self.assertEqual(view["version_check"]["requested"], False)

    def test_full_table_rejects_per_date_known_versions(self):
        cursor = _HiveCursor(
            describe_rows=HIVE_DESCRIBE_FULL,
            table_version="1",
            rows_by_date={None: []},
            select_names=["branch_code", "branch_name"],
        )
        service = self._service(cursor, HIVE_DESCRIBE_FULL)
        submitted = service.submit_hive(
            "dim", "branch_info", ["2026-09-11"], known_versions={"2026-09-11": "1"}
        )
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "failed")
        self.assertIn("known_version", view["error"])


class KnownVersionValidationTests(unittest.TestCase):
    def test_normalize_known_versions_rejects_unrequested_dates(self):
        with self.assertRaises(LoadJobError) as ctx:
            normalize_known_versions({"2026-09-13": "1"}, ["2026-09-11"])
        self.assertIn("2026-09-13", str(ctx.exception))

    def test_normalize_known_versions_rejects_bad_shape_and_values(self):
        with self.assertRaises(LoadJobError):
            normalize_known_versions({"2026-9-1": "1"}, ["2026-09-01"])
        with self.assertRaises(LoadJobError):
            normalize_known_versions({"2026-09-11": "  "}, ["2026-09-11"])
        with self.assertRaises(LoadJobError):
            normalize_known_versions(["2026-09-11"], ["2026-09-11"])

    def test_normalize_known_versions_empty_is_no_comparison(self):
        self.assertEqual(normalize_known_versions(None, ["2026-09-11"]), {})
        self.assertEqual(normalize_known_versions({}, ["2026-09-11"]), {})

    def test_normalize_known_version_blank_is_rejected(self):
        self.assertIsNone(normalize_known_version(None))
        self.assertEqual(normalize_known_version(" 1789615800 "), "1789615800")
        with self.assertRaises(LoadJobError):
            normalize_known_version("")

    def test_resolve_version_status_direction(self):
        self.assertEqual(resolve_version_status(None, "1"), "modified")
        self.assertEqual(resolve_version_status("1", None), "unknown")
        self.assertEqual(resolve_version_status("1", "1"), "not_modified")
        self.assertEqual(resolve_version_status("1", "2"), "modified")

    def test_build_version_check_counts(self):
        self.assertEqual(
            build_version_check(["modified", "not_modified", "unknown", "modified"], requested=True),
            {"requested": True, "not_modified": 1, "modified": 2, "unknown": 1},
        )

    def test_partition_describe_sql_is_metadata_only(self):
        sql = build_partition_describe_sql("db8", "balance", "etl_date", "2026-09-11")
        self.assertEqual(
            sql,
            "DESCRIBE FORMATTED `db8`.`balance` PARTITION (`etl_date`='2026-09-11')",
        )
        self.assertNotIn("SELECT", sql)


if __name__ == "__main__":
    unittest.main()
