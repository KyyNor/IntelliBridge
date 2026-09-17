"""声明式 Load API：MySQL/Hive 整表或逻辑日期分区 → Parquet（issue #1）。

与现有 query 能力的分工（query 保持不变）：
    query = SQL / 小结果 / JSON（tools/mysql_query.py、tools/hive_query.py）
    load  = table + 逻辑日期 / 大数据抽取 / Parquet（本模块）

核心原则：Hive 只接受逻辑 dates[]。etl_date/cdate 物理分区字段由本模块根据
Hive metadata 自动适配，不向调用方暴露；两个候选字段并存时按平台配置
hive.primary_time_partition 选择主字段，不由调用方决定。
Taosha 侧业务规则（近一年任意日 / 历史仅自然月末）属于 Taosha #16，不在本层实现。
"""

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

import pymysql
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from utils.cache import cache
from utils.config import config
from utils.hive_errors import normalize_hive_error
from utils.hive_pool import hive_pool
from utils.load_jobs import LoadJobError, LoadJobManager
from utils.logger import logger
from utils.mysql_pool import mysql_pool
from utils.parquet_export import (
    DEFAULT_BATCH_ROWS,
    hive_arrow_type,
    mysql_arrow_type,
    stream_to_parquet,
)

router = APIRouter(prefix="/api/load", tags=["Load"])

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
TIME_PARTITION_FIELDS = ("etl_date", "cdate")
FULL_TABLE_NOTICE = "该表没有时间分区，本次返回全量数据"
MAX_REQUEST_DATES = 500

# load 自身的 DESCRIBE 串行化（与 hive_query 的 describe 缓存互不影响）
_describe_lock = threading.RLock()


class MySqlLoadRequest(BaseModel):
    database_id: str
    table: str


class HiveLoadRequest(BaseModel):
    database: str
    table: str
    dates: Optional[List[str]] = None


# ==================== 纯函数：DESCRIBE / SHOW PARTITIONS 解析 ====================

def parse_describe_rows(rows) -> dict:
    """解析 DESCRIBE 输出为数据列与分区列（Spark Thrift 含 '# Partition Information' 段）。"""
    data_columns, partition_columns = [], []
    section = "data"
    for row in rows or []:
        name = (row[0] or "").strip() if len(row) > 0 else ""
        dtype = (row[1] or "").strip() if len(row) > 1 else ""
        comment = (row[2] or "").strip() if len(row) > 2 else ""
        if name.startswith("#"):
            lowered = name.lower()
            if "col_name" in lowered or "data_type" in lowered:
                pass  # 分区段内的列表头行，不改变当前 section
            elif "partition" in lowered:
                section = "partition"
            else:
                section = "other"  # Detailed Table Information 等后续段落
            continue
        if not name:
            continue
        entry = {"name": name, "type": dtype, "comment": comment}
        if section == "data":
            data_columns.append(entry)
        elif section == "partition":
            partition_columns.append(entry)
    return {"columns": data_columns, "partition_columns": partition_columns}


def resolve_time_partition_field(partition_columns, primary_hint: str) -> Optional[str]:
    """在分区列中识别 etl_date/cdate（大小写不敏感）；并存时按平台配置选主字段。

    返回实际物理字段名（保留原大小写）；无时间分区返回 None。
    """
    by_lower = {c["name"].lower(): c["name"] for c in partition_columns or []}
    candidates = [f for f in TIME_PARTITION_FIELDS if f in by_lower]
    if not candidates:
        return None
    if len(candidates) > 1:
        hint = (primary_hint or "").lower()
        if hint in candidates:
            return by_lower[hint]
        logger.warning(
            f"表同时存在 {candidates} 时间分区，配置主字段 {primary_hint!r} 无效，回退 {candidates[0]}"
        )
    return by_lower[candidates[0]]


def parse_partition_rows(rows, field: str) -> set:
    """解析 SHOW PARTITIONS 输出，返回目标分区字段的取值集合（多级分区取该字段值）。"""
    values = set()
    target = field.lower()
    for row in rows or []:
        spec = row[0] if row else ""
        for kv in str(spec or "").split("/"):
            if "=" not in kv:
                continue
            key, _, value = kv.partition("=")
            if key.strip().lower() == target:
                values.add(value.strip())
    return values


def build_hive_sql(database: str, table: str, partition_field: Optional[str], dates: List[str]) -> str:
    if not partition_field:
        return f"SELECT * FROM `{database}`.`{table}`"
    ordered = sorted(dates)
    values = ", ".join("'" + d + "'" for d in ordered)
    return f"SELECT * FROM `{database}`.`{table}` WHERE `{partition_field}` IN ({values})"


def build_hive_file_name(table: str, partition_field: Optional[str], actual_dates: List[str]) -> str:
    """时间分区表在文件名中表达实际逻辑日期；full_table 文件名绝不能带请求日期。"""
    if not partition_field or not actual_dates:
        return f"{table}.parquet"
    ordered = sorted(actual_dates)
    if len(ordered) == 1:
        return f"{table}__{ordered[0]}.parquet"
    return f"{table}__{ordered[0]}__{ordered[-1]}.parquet"


def normalize_requested_dates(dates) -> List[str]:
    """校验并去重排序逻辑日期；非法格式/非法日历日期直接拒绝。"""
    if not dates:
        return []
    if len(dates) > MAX_REQUEST_DATES:
        raise LoadJobError(f"一次请求最多 {MAX_REQUEST_DATES} 个日期")
    seen = set()
    for d in dates:
        if not isinstance(d, str) or not DATE_PATTERN.match(d):
            raise LoadJobError(f"非法日期（应为 YYYY-MM-DD）: {d!r}")
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError as exc:
            raise LoadJobError(f"非法日期: {d!r}") from exc
        seen.add(d)
    return sorted(seen)


# ==================== 服务编排 ====================

class LoadExportService:
    """Load 编排：解析表结构 → 分批抽取 → Parquet spool → job 元数据。"""

    def __init__(
        self,
        manager: Optional[LoadJobManager] = None,
        *,
        mysql_connection: Optional[Callable] = None,
        hive_connection: Optional[Callable] = None,
        mysql_describe: Optional[Callable] = None,
        hive_describe: Optional[Callable] = None,
    ):
        self.manager = manager or load_job_manager
        # 连接工厂返回上下文管理器（测试注入用）
        self._mysql_connection = mysql_connection or mysql_pool.get_connection
        self._hive_connection = hive_connection or hive_pool.get_connection
        self._mysql_describe = mysql_describe or self._default_mysql_describe
        self._hive_describe = hive_describe or self._default_hive_describe

    # ---------- 对外入口 ----------

    def submit_mysql(self, database_id: str, table: str) -> dict:
        if not IDENTIFIER_PATTERN.match(table or ""):
            return {"error": f"非法表名: {table!r}"}
        if not mysql_pool.get_node_by_database(database_id or ""):
            return {"error": f"数据库不存在: {database_id}"}
        return self.manager.submit(
            "mysql", {"database_id": database_id, "table": table}, self._run_mysql
        )

    def submit_hive(self, database: str, table: str, dates) -> dict:
        if not IDENTIFIER_PATTERN.match(database or ""):
            return {"error": f"非法库名: {database!r}"}
        if not IDENTIFIER_PATTERN.match(table or ""):
            return {"error": f"非法表名: {table!r}"}
        try:
            requested = normalize_requested_dates(dates)
        except LoadJobError as exc:
            return {"error": str(exc)}
        return self.manager.submit(
            "hive",
            {"database": database, "table": table, "requested_dates": requested},
            self._run_hive,
        )

    def hive_profile(self, database: str, table: str) -> dict:
        """轻量 load profile：Taosha 在应用 #16 规则前只需知道 time_partitioned/full_table。"""
        if not IDENTIFIER_PATTERN.match(database or ""):
            return {"error": f"非法库名: {database!r}"}
        if not IDENTIFIER_PATTERN.match(table or ""):
            return {"error": f"非法表名: {table!r}"}
        described = self._hive_describe(database, table)
        if "error" in described:
            return described
        field = resolve_time_partition_field(
            described.get("partition_columns"), self._primary_time_partition()
        )
        return {
            "database": database,
            "table": table,
            "load_mode": "time_partitioned" if field else "full_table",
            "time_partitioned": bool(field),
        }

    # ---------- MySQL ----------

    def _default_mysql_describe(self, database_id: str, table: str) -> dict:
        from tools.mysql_query import mysql_query

        # 复用现有 describe（information_schema + 30 分钟缓存）
        return mysql_query.describe_table(database_id, table)

    def _run_mysql(self, ctx) -> dict:
        params = ctx.params
        database_id, table = params["database_id"], params["table"]
        described = self._mysql_describe(database_id, table)
        if "error" in described:
            raise LoadJobError(str(described["error"]))
        declared = {
            c["column_name"]: c["column_type"] for c in described.get("columns", [])
        }
        actual_db = mysql_pool.get_database_by_unique_id(database_id) or database_id
        out_path = ctx.work_dir / f"{table}.parquet"
        sql = f"SELECT * FROM `{table}`"
        logger.info(f"MySQL load 导出: {database_id} ({actual_db}) {sql}")
        with self._mysql_connection(database_id) as conn:
            # SSCursor(unbuffered)：服务端流式读取，配合 fetchmany 控制内存。
            cursor = conn.cursor(pymysql.cursors.SSDictCursor)
            try:
                cursor.execute(sql)
                names = [d[0] for d in cursor.description or []]
                row_count, resolved = stream_to_parquet(
                    cursor.fetchmany,
                    [(n, declared.get(n)) for n in names],
                    out_path,
                    batch_rows=self._batch_rows(),
                    should_stop=ctx.check_alive,
                    row_to_values=lambda row, names=tuple(names): [row.get(n) for n in names],
                    declared_to_arrow=mysql_arrow_type,
                )
            finally:
                cursor.close()
        record = ctx.add_file(out_path.name)
        ctx.set_result(
            source={
                "type": "mysql",
                "database_id": database_id,
                "database": actual_db,
                "table": table,
            },
            schema=[{"name": name, "type": str(t)} for name, t in resolved],
            row_count=row_count,
            source_version=None,
            load_mode="full_table",
            time_partitioned=False,
            requested_dates=[],
            actual_dates=[],
            notice=None,
        )
        return {}

    # ---------- Hive ----------

    def _default_hive_describe(self, database: str, table: str) -> dict:
        """DESCRIBE 表结构（带 30 分钟缓存）；只依赖 HiveServer2，不依赖 metastore 库。"""
        key = f"load:describe:{database}.{table}"
        cached = cache.get(key)
        if cached is not None:
            return cached
        with _describe_lock:
            cached = cache.get(key)
            if cached is not None:
                return cached
            try:
                with self._hive_connection() as conn:
                    cursor = conn.cursor()
                    try:
                        cursor.execute(f"DESCRIBE `{database}`.`{table}`")
                        rows = cursor.fetchall()
                    finally:
                        cursor.close()
            except Exception as exc:
                normalized = normalize_hive_error(exc)
                return {"error": f"查询表结构失败: {normalized['message']}"}
            parsed = parse_describe_rows(rows)
            if not parsed["columns"] and not parsed["partition_columns"]:
                return {"error": f"表 {database}.{table} 不存在或无字段"}
            cache.set(key, parsed, expire=1800)
            return parsed

    def _show_partitions(self, database: str, table: str, field: str) -> set:
        try:
            with self._hive_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(f"SHOW PARTITIONS `{database}`.`{table}`")
                    rows = cursor.fetchall()
                finally:
                    cursor.close()
        except Exception as exc:
            normalized = normalize_hive_error(exc)
            raise LoadJobError(f"查询分区列表失败: {normalized['message']}")
        return parse_partition_rows(rows, field)

    def _run_hive(self, ctx) -> dict:
        params = ctx.params
        database, table = params["database"], params["table"]
        requested = params.get("requested_dates") or []
        described = self._hive_describe(database, table)
        if "error" in described:
            raise LoadJobError(str(described["error"]))
        partition_columns = described.get("partition_columns", [])
        field = resolve_time_partition_field(partition_columns, self._primary_time_partition())

        actual_dates: List[str] = []
        notice = None
        if field is None:
            # 无时间分区：忽略日期过滤，整表导出；明确告知全量语义
            load_mode = "full_table"
            notice = FULL_TABLE_NOTICE
        else:
            load_mode = "time_partitioned"
            if not requested:
                raise LoadJobError(
                    f"表 {database}.{table} 是时间分区表，必须至少传一个逻辑日期（dates）"
                )
            available = self._show_partitions(database, table, field)
            missing = [d for d in requested if d not in available]
            if missing:
                preview = ", ".join(missing[:20]) + ("..." if len(missing) > 20 else "")
                raise LoadJobError(f"以下日期分区在表中不存在: {preview}")
            actual_dates = list(requested)

        sql = build_hive_sql(database, table, field, actual_dates)
        file_name = build_hive_file_name(table, field, actual_dates)
        out_path = ctx.work_dir / file_name
        declared = {
            c["name"]: c["type"]
            for c in described.get("columns", []) + partition_columns
        }
        logger.info(f"Hive load 导出 ({load_mode}): {database}.{table} {sql}")
        with self._hive_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                names = [d[0] for d in cursor.description or []]
                lowered = {k.lower(): v for k, v in declared.items()}
                row_count, resolved = stream_to_parquet(
                    cursor.fetchmany,
                    [(n, lowered.get(str(n).lower())) for n in names],
                    out_path,
                    batch_rows=self._batch_rows(),
                    should_stop=ctx.check_alive,
                    declared_to_arrow=hive_arrow_type,
                )
            finally:
                cursor.close()
        ctx.add_file(file_name)
        ctx.set_result(
            source={"type": "hive", "database": database, "table": table},
            schema=[{"name": name, "type": str(t)} for name, t in resolved],
            row_count=row_count,
            source_version=None,
            load_mode=load_mode,
            time_partitioned=field is not None,
            requested_dates=requested,
            actual_dates=actual_dates,
            notice=notice,
        )
        return {}

    # ---------- 配置 ----------

    def _primary_time_partition(self) -> str:
        return str(config.get("hive.primary_time_partition", "etl_date"))

    def _batch_rows(self) -> int:
        return max(1000, int(config.get("load.batch_rows", DEFAULT_BATCH_ROWS)))


def _default_manager() -> LoadJobManager:
    root = Path(config.get("load.spool_root", "data/load_spool"))
    if not root.is_absolute():
        root = Path(__file__).resolve().parent.parent / root
    return LoadJobManager(
        root,
        ttl_seconds=float(config.get("load.ttl_seconds", 3600)),
        sweep_interval_seconds=float(config.get("load.sweep_interval_seconds", 300)),
        max_workers=max(1, int(config.get("load.max_concurrent_jobs", 2))),
        job_timeout_seconds=float(config.get("load.job_timeout_seconds", 1800)),
    )


load_job_manager = _default_manager()
load_export = LoadExportService(load_job_manager)


# ==================== API 路由 ====================

@router.post("/mysql")
def create_mysql_load(request: MySqlLoadRequest):
    """提交 MySQL 整表导出任务（Parquet）"""
    result = load_export.submit_mysql(request.database_id, request.table)
    return {"data": result}


@router.post("/hive")
def create_hive_load(request: HiveLoadRequest):
    """提交 Hive 逻辑日期导出任务（Parquet）"""
    result = load_export.submit_hive(request.database, request.table, request.dates)
    return {"data": result}


@router.get("/hive/profile")
def get_hive_profile(database: str, table: str):
    """查询 Hive 表的 load profile（time_partitioned / full_table，不含物理字段名）"""
    return {"data": load_export.hive_profile(database, table)}


@router.get("/{job_id}")
def get_load_job(job_id: str):
    """查询 load job 状态（pending/running/ready/failed）"""
    view = load_job_manager.get_view(job_id)
    if view is None:
        return {"data": {"error": f"任务不存在: {job_id}"}}
    return {"data": view}


@router.get("/{job_id}/files/{file_id}")
def download_load_file(job_id: str, file_id: str):
    """下载导出的 Parquet 文件"""
    opened = load_job_manager.open_file(job_id, file_id)
    if opened is None:
        raise HTTPException(status_code=404, detail="文件不存在或已清理")
    path, record = opened
    return FileResponse(path, media_type="application/octet-stream", filename=record["name"])


@router.delete("/{job_id}")
def delete_load_job(job_id: str):
    """主动清理 load job（取消运行中的任务并删除文件）"""
    result = load_job_manager.delete(job_id)
    if result is None:
        return {"data": {"error": f"任务不存在: {job_id}"}}
    return {"data": result}
