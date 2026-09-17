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

import tools.load_export as load_export_module
from tools.load_export import LoadExportService
from utils.load_jobs import LoadJobManager

try:
    import pyarrow  # noqa: F401
    PYARROW_OK = True
except ImportError:
    PYARROW_OK = False


MYSQL_DESCRIBE = {
    "columns": [
        {"column_name": "id", "column_type": "bigint"},
        {"column_name": "name", "column_type": "varchar(50)"},
        {"column_name": "balance", "column_type": "decimal(10,2)"},
        {"column_name": "created_at", "column_type": "datetime"},
    ],
    "total": 4,
}

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


class _FakeCursor:
    """按 SQL 语句脚本化返回结果的假游标。"""

    def __init__(self, results, select_names=None, description_types=None):
        self.results = results  # list[(sql_keyword, rows)]
        self.executed = []
        self.select_names = select_names or []
        self.description_types = description_types or {}

    def execute(self, sql):
        self.executed.append(sql)
        self._pending = []
        for keyword, rows in self.results:
            if keyword in sql:
                self._pending = rows
                return
        raise AssertionError(f"未脚本化的 SQL: {sql}")

    def fetchall(self):
        return self._pending

    def fetchmany(self, size):
        batch, self._pending = self._pending[:size], self._pending[size:]
        return batch

    @property
    def description(self):
        return [
            (n, self.description_types.get(n), None, None, None, None, None)
            for n in self.select_names
        ]

    def close(self):
        pass


class _FakeMySQLCursor(_FakeCursor):
    def __init__(self, rows):
        names = ["id", "name", "balance", "created_at"]
        super().__init__([("SELECT", rows)], select_names=names)
        self.cursorclass_arg = None


class _FakeMySQLConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, cursorclass=None):
        self._cursor.cursorclass_arg = cursorclass
        return self._cursor


class _FakeHiveConn:
    """假 Hive 连接：同一脚本化游标按语句依次执行。"""

    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class _StubMySQLPool:
    """替身 mysql_pool：只覆盖 load 用到的方法。"""

    def __init__(self, actual_db="customer_db"):
        self.actual_db = actual_db

    def get_node_by_database(self, database_id):
        return {"name": "node"} if database_id == "mysql_121_customer_db" else None

    def get_database_by_unique_id(self, database_id):
        return self.actual_db if database_id == "mysql_121_customer_db" else None


def _wait_view(manager, job_id, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = manager.get_view(job_id)
        if view is not None and view["status"] in ("ready", "failed"):
            return view
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内进入终态")


@unittest.skipUnless(PYARROW_OK, "pyarrow 未安装")
class LoadExportServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "spool"
        self.manager = LoadJobManager(self.root, ttl_seconds=3600, sweep_interval_seconds=9999)
        self._original_pool = load_export_module.mysql_pool
        load_export_module.mysql_pool = _StubMySQLPool()
        self.hive_results = []

    def tearDown(self):
        load_export_module.mysql_pool = self._original_pool
        self.manager.stop()
        self.tmp.cleanup()

    def _service(self, *, mysql_conn=None, hive_conn=None, mysql_describe=None, hive_describe=None):
        return LoadExportService(
            self.manager,
            mysql_connection=mysql_conn or self._default_mysql_conn,
            hive_connection=hive_conn or self._default_hive_conn,
            mysql_describe=mysql_describe,
            hive_describe=hive_describe or (lambda db, tbl: dict(self._hive_describe)),
        )

    @contextmanager
    def _default_mysql_conn(self, database_id):
        yield self._mysql_conn

    @contextmanager
    def _default_hive_conn(self, timeout=None):
        yield _FakeHiveConn(self._hive_conn)

    # ---------- MySQL ----------

    def test_mysql_full_table_load(self):
        import datetime
        import pyarrow.parquet as pq

        rows = [
            {"id": 1, "name": "alice", "balance": 12.5, "created_at": datetime.datetime(2026, 9, 1)},
            {"id": 2, "name": None, "balance": None, "created_at": None},
            {"id": 3, "name": "bob", "balance": 0.01, "created_at": datetime.datetime(2026, 9, 2)},
        ]
        self._mysql_conn = _FakeMySQLConn(_FakeMySQLCursor(rows))
        service = self._service(mysql_describe=lambda db_id, tbl: dict(MYSQL_DESCRIBE))

        submitted = service.submit_mysql("mysql_121_customer_db", "customer")
        self.assertIsNone(submitted.get("error"))
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "ready", view.get("error"))
        self.assertEqual(view["row_count"], 3)
        self.assertEqual(view["load_mode"], "full_table")
        self.assertFalse(view["time_partitioned"])
        self.assertEqual(view["source"], {
            "type": "mysql", "database_id": "mysql_121_customer_db",
            "database": "customer_db", "table": "customer",
        })
        # SQL 是整表全字段，不接受任意 SQL
        self.assertEqual(self._mysql_conn._cursor.executed, ["SELECT * FROM `customer`"])
        # 使用流式游标（SSDictCursor）而不是全量缓冲游标
        import pymysql
        self.assertIs(self._mysql_conn._cursor.cursorclass_arg, pymysql.cursors.SSDictCursor)
        # Parquet 内容与元数据
        record = view["files"][0]
        self.assertEqual(record["name"], "customer.parquet")
        opened = self.manager.open_file(view["job_id"], record["id"])
        table = pq.read_table(opened[0])
        self.assertEqual(table.num_rows, 3)
        self.assertEqual(table.column("id").to_pylist(), [1, 2, 3])
        self.assertIsNone(table.column("name").to_pylist()[1])
        schema_names = [c["name"] for c in view["schema"]]
        self.assertEqual(schema_names, ["id", "name", "balance", "created_at"])

    def test_mysql_rejects_unknown_database_and_bad_table_name(self):
        service = self._service()
        self.assertIn("error", service.submit_mysql("nope", "customer"))
        self.assertIn("error", service.submit_mysql("mysql_121_customer_db", "customer;drop"))

    def test_mysql_describe_error_fails_job(self):
        self._mysql_conn = _FakeMySQLConn(_FakeMySQLCursor([]))
        service = self._service(mysql_describe=lambda db_id, tbl: {"error": f"表 {tbl} 不存在"})
        submitted = service.submit_mysql("mysql_121_customer_db", "missing")
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "failed")
        self.assertIn("不存在", view["error"])
        # 失败 job 不产出文件
        self.assertEqual(view["files"], [])
        self.assertEqual(view["byte_size"], 0)

    # ---------- Hive ----------

    def _hive_service(self, describe, select_rows, extra_results=()):
        self._hive_describe = describe
        results = list(extra_results) + [
            ("SELECT", select_rows),
        ]
        self._hive_conn = _FakeCursor(
            results,
            select_names=[c["name"] for c in describe["columns"] + describe["partition_columns"]],
        )
        return self._service()

    def test_hive_time_partitioned_etl_date(self):
        service = self._hive_service(
            HIVE_DESCRIBE_ETL,
            [(1, "alice", "2026-09-11"), (2, "bob", "2026-09-11"), (3, None, "2026-09-12")],
            extra_results=[
                ("SHOW PARTITIONS", [("etl_date=2026-09-11",), ("etl_date=2026-09-12",)]),
            ],
        )
        submitted = service.submit_hive("db8", "balance", ["2026-09-12", "2026-09-11"])
        self.assertIsNone(submitted.get("error"))
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "ready", view.get("error"))
        self.assertEqual(view["load_mode"], "time_partitioned")
        self.assertTrue(view["time_partitioned"])
        self.assertEqual(view["requested_dates"], ["2026-09-11", "2026-09-12"])
        self.assertEqual(view["actual_dates"], ["2026-09-11", "2026-09-12"])
        self.assertIsNone(view["notice"])
        self.assertEqual(view["row_count"], 3)
        self.assertEqual(view["files"][0]["name"], "balance__2026-09-11__2026-09-12.parquet")
        select_sql = [s for s in self._hive_conn.executed if s.startswith("SELECT")][0]
        self.assertEqual(
            select_sql,
            "SELECT * FROM `db8`.`balance` WHERE `etl_date` IN ('2026-09-11', '2026-09-12')",
        )

    def test_hive_time_partitioned_cdate_case_insensitive(self):
        service = self._hive_service(
            HIVE_DESCRIBE_CDATE,
            [(1, "2026-09-12")],
            extra_results=[("SHOW PARTITIONS", [("cdate=2026-09-12",)])],
        )
        submitted = service.submit_hive("db8", "balance", ["2026-09-12"])
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "ready", view.get("error"))
        select_sql = [s for s in self._hive_conn.executed if s.startswith("SELECT")][0]
        self.assertIn("WHERE `CDATE` IN ('2026-09-12')", select_sql)
        self.assertEqual(view["files"][0]["name"], "balance__2026-09-12.parquet")

    def test_hive_missing_partition_fails_with_explicit_dates(self):
        service = self._hive_service(
            HIVE_DESCRIBE_ETL,
            [],
            extra_results=[("SHOW PARTITIONS", [("etl_date=2026-09-11",)])],
        )
        submitted = service.submit_hive("db8", "balance", ["2026-09-11", "2026-09-13", "2026-09-14"])
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "failed")
        self.assertIn("2026-09-13", view["error"])
        self.assertIn("2026-09-14", view["error"])
        self.assertNotIn("2026-09-11", view["error"])
        self.assertEqual(view["files"], [])

    def test_hive_time_partitioned_requires_dates(self):
        service = self._hive_service(HIVE_DESCRIBE_ETL, [])
        submitted = service.submit_hive("db8", "balance", [])
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "failed")
        self.assertIn("至少传一个逻辑日期", view["error"])

    def test_hive_full_table_ignores_dates_and_keeps_notice(self):
        service = self._hive_service(
            HIVE_DESCRIBE_FULL,
            [("B001", "武汉"), ("B002", "上海")],
        )
        submitted = service.submit_hive("dim", "branch_info", ["2026-09-12"])
        self.assertIsNone(submitted.get("error"))
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "ready", view.get("error"))
        self.assertEqual(view["load_mode"], "full_table")
        self.assertFalse(view["time_partitioned"])
        self.assertEqual(view["requested_dates"], ["2026-09-12"])
        self.assertEqual(view["actual_dates"], [])
        self.assertEqual(view["notice"], "该表没有时间分区，本次返回全量数据")
        # 文件名绝不能带请求日期，避免伪快照语义
        self.assertEqual(view["files"][0]["name"], "branch_info.parquet")
        select_sql = [s for s in self._hive_conn.executed if s.startswith("SELECT")][0]
        self.assertEqual(select_sql, "SELECT * FROM `dim`.`branch_info`")

    def test_hive_rejects_bad_date_at_submit(self):
        service = self._hive_service(HIVE_DESCRIBE_ETL, [])
        self.assertIn("error", service.submit_hive("db8", "balance", ["2026-9-1"]))
        self.assertIn("error", service.submit_hive("db8", "balance", ["2026-02-30"]))
        self.assertIn("error", service.submit_hive("db8;drop", "balance", []))

    def test_hive_profile_returns_logical_mode_without_physical_field(self):
        service = self._hive_service(HIVE_DESCRIBE_ETL, [])
        profile = service.hive_profile("db8", "balance")
        self.assertEqual(profile["load_mode"], "time_partitioned")
        self.assertTrue(profile["time_partitioned"])
        # 不暴露 etl_date/cdate 物理字段名
        serialized = repr(profile)
        self.assertNotIn("etl_date", serialized)
        self.assertNotIn("cdate", serialized)

        self._hive_describe = HIVE_DESCRIBE_FULL
        profile = service.hive_profile("dim", "branch_info")
        self.assertEqual(profile["load_mode"], "full_table")
        self.assertFalse(profile["time_partitioned"])

    def test_hive_describe_error_propagates_to_profile(self):
        service = self._service(hive_describe=lambda db, tbl: {"error": "查询表结构失败: 表不存在"})
        profile = service.hive_profile("db8", "missing")
        self.assertIn("error", profile)


if __name__ == "__main__":
    unittest.main()
