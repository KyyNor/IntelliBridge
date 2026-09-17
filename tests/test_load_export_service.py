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

    def __init__(self, results, select_names=None, description_types=None, per_date_rows=None):
        self.results = results  # list[(sql_keyword, rows)]
        self.executed = []
        self.select_names = select_names or []
        self.description_types = description_types or {}
        # 按 SQL 中的日期字面量分别返回行（分区即文件场景）
        self.per_date_rows = per_date_rows or {}

    def execute(self, sql):
        self.executed.append(sql)
        self._pending = []
        for date, rows in self.per_date_rows.items():
            if f"'{date}'" in sql:
                self._pending = rows
                return
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

    def _hive_service(self, describe, select_rows, extra_results=(), per_date_rows=None):
        self._hive_describe = describe
        results = list(extra_results) + [
            ("SELECT", select_rows),
        ]
        self._hive_conn = _FakeCursor(
            results,
            select_names=[c["name"] for c in describe["columns"] + describe["partition_columns"]],
            per_date_rows=per_date_rows,
        )
        return self._service()

    def test_hive_time_partitioned_multiple_dates_produce_per_date_files(self):
        """分区即文件：每个 logical date 独立文件 + 独立 logical_date 身份。"""
        import pyarrow.parquet as pq

        service = self._hive_service(
            HIVE_DESCRIBE_ETL,
            [],
            extra_results=[
                ("SHOW PARTITIONS", [("etl_date=2026-09-11",), ("etl_date=2026-09-12",)]),
            ],
            per_date_rows={
                "2026-09-11": [(1, "alice", "2026-09-11")],
                "2026-09-12": [(2, "bob", "2026-09-12"), (3, None, "2026-09-12")],
            },
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

        # 逐日一个文件，文件名与 logical_date 身份一致且升序
        files = view["files"]
        self.assertEqual(len(files), 2, files)
        self.assertEqual([f["name"] for f in files],
                         ["balance__2026-09-11.parquet", "balance__2026-09-12.parquet"])
        self.assertEqual([f["logical_date"] for f in files], ["2026-09-11", "2026-09-12"])
        for f in files:
            self.assertEqual(f["format"], "parquet")
            self.assertTrue(f["sha256"])
            self.assertGreater(f["size"], 0)

        # 每个文件只含对应日期的数据
        expected_rows = {"2026-09-11": 1, "2026-09-12": 2}
        for f in files:
            opened = self.manager.open_file(view["job_id"], f["id"])
            table = pq.read_table(opened[0])
            self.assertEqual(table.num_rows, expected_rows[f["logical_date"]], f["name"])
            self.assertEqual(set(table.column("etl_date").to_pylist()), {f["logical_date"]})

        # 每个分区用等值谓词单独查询，不用 IN 合并
        selects = [s for s in self._hive_conn.executed if s.startswith("SELECT")]
        self.assertEqual(
            selects,
            [
                "SELECT * FROM `db8`.`balance` WHERE `etl_date` = '2026-09-11'",
                "SELECT * FROM `db8`.`balance` WHERE `etl_date` = '2026-09-12'",
            ],
        )

    def test_hive_primary_time_partition_invalid_fails_closed(self):
        """etl_date/cdate 并存且配置非法 → 明确失败，不猜测字段。"""
        both = {
            "columns": [{"name": "id", "type": "bigint", "comment": ""}],
            "partition_columns": [
                {"name": "etl_date", "type": "string", "comment": ""},
                {"name": "cdate", "type": "string", "comment": ""},
            ],
        }
        self._hive_describe = both
        self._hive_conn = _FakeCursor([], select_names=["id"])
        service = self._service(hive_describe=lambda db, tbl: dict(both))
        service._primary_time_partition = lambda: "biz_date"  # 配置不指向任一候选
        submitted = service.submit_hive("db8", "balance", ["2026-09-11"])
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "failed")
        self.assertIn("primary_time_partition", view["error"])
        self.assertEqual(view["files"], [])
        # 只读 profile 同样明确失败，不返回猜测的 load_mode
        profile = service.hive_profile("db8", "balance")
        self.assertIn("error", profile)
        self.assertIn("primary_time_partition", profile["error"])

    def test_hive_both_fields_with_valid_primary_config(self):
        """并存但配置合法：按配置选择（导出成功，物理字段不外泄）。"""
        both = {
            "columns": [{"name": "id", "type": "bigint", "comment": ""}],
            "partition_columns": [
                {"name": "etl_date", "type": "string", "comment": ""},
                {"name": "cdate", "type": "string", "comment": ""},
            ],
        }
        # SELECT * 会同时返回数据列与被选中物理分区列
        service = self._hive_service(
            both,
            [(1, "2026-09-11", "2026-09-11")],
            extra_results=[("SHOW PARTITIONS", [("cdate=2026-09-11",)])],
        )
        service._primary_time_partition = lambda: "cdate"
        submitted = service.submit_hive("db8", "balance", ["2026-09-11"])
        view = _wait_view(self.manager, submitted["job_id"])
        self.assertEqual(view["status"], "ready", view.get("error"))
        selects = [s for s in self._hive_conn.executed if s.startswith("SELECT")]
        self.assertEqual(selects, ["SELECT * FROM `db8`.`balance` WHERE `cdate` = '2026-09-11'"])
        self.assertEqual(view["files"][0]["logical_date"], "2026-09-11")
        # profile 只回逻辑模式
        self.assertNotIn("etl_date", repr(service.hive_profile("db8", "balance")))

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
        self.assertIn("WHERE `CDATE` = '2026-09-12'", select_sql)
        self.assertEqual(view["files"][0]["name"], "balance__2026-09-12.parquet")
        self.assertEqual(view["files"][0]["logical_date"], "2026-09-12")

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
