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
    """在分区列中识别 etl_date/cdate（大小写不敏感）。

    返回实际物理字段名（保留原大小写）；无时间分区返回 None。

    两个候选字段并存时由平台配置 `hive.primary_time_partition` 选主字段——
    这属于数据口径选择：配置缺失或无效时**明确失败（fail-closed）**，
    绝不 warning 后猜测某个字段（会静默取错数据口径）。
    """
    by_lower = {c["name"].lower(): c["name"] for c in partition_columns or []}
    candidates = [f for f in TIME_PARTITION_FIELDS if f in by_lower]
    if not candidates:
        return None
    if len(candidates) > 1:
        hint = (primary_hint or "").lower()
        if hint not in candidates:
            raise LoadJobError(
                f"表同时存在 {candidates} 两个时间分区，但配置 "
                f"hive.primary_time_partition={primary_hint!r} 不是其中之一；"
                "由平台配置指定主时间字段，拒绝按默认值猜测（避免取错数据口径）"
            )
        return by_lower[hint]
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
    if not dates:
        raise LoadJobError("时间分区表导出必须指定至少一个逻辑日期")
    ordered = sorted(dates)
    values = ", ".join("'" + d + "'" for d in ordered)
    return f"SELECT * FROM `{database}`.`{table}` WHERE `{partition_field}` IN ({values})"


def build_partitioned_hive_sql(database: str, table: str, partition_field: str, date: str) -> str:
    """单个 logical date → 单个物理分区谓词（等值，不用 IN）。"""
    return f"SELECT * FROM `{database}`.`{table}` WHERE `{partition_field}` = '{date}'"


def build_hive_file_name(table: str, partition_field: Optional[str], logical_dates: List[str]) -> str:
    """时间分区表在文件名中表达实际逻辑日期；full_table 文件名绝不能带请求日期。

    时间分区表按逻辑日期**逐日一个文件**（分区即文件），因此逻辑日期必须是
    单一日期。
    """
    if not partition_field or not logical_dates:
        return f"{table}.parquet"
    if len(logical_dates) != 1:
        raise LoadJobError(
            "时间分区表导出必须按逻辑日期逐日产出文件（分区即文件），"
            f"收到 {len(logical_dates)} 个日期"
        )
    return f"{table}__{logical_dates[0]}.parquet"


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
        try:
            field = resolve_time_partition_field(
                described.get("partition_columns"), self._primary_time_partition()
            )
        except LoadJobError as exc:
            # 并存的物理时间字段无法按配置判定 → 明确失败，不返回猜测的 profile
            return {"error": str(exc)}
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

        declared = {
            c["name"]: c["type"]
            for c in described.get("columns", []) + partition_columns
        }
        lowered = {k.lower(): v for k, v in declared.items()}

        if field is None:
            # 无时间分区：忽略日期过滤，整表导出为单个文件；明确告知全量语义。
            # 文件名绝不带请求日期，避免伪快照语义。
            load_mode = "full_table"
            notice = FULL_TABLE_NOTICE
            file_name = build_hive_file_name(table, None, [])
            row_count, resolved = self._export_one(
                ctx,
                build_hive_sql(database, table, None, []),
                file_name,
                lowered,
                label=f"full_table:{database}.{table}",
            )
            ctx.add_file(file_name)
            ctx.set_result(
                source={"type": "hive", "database": database, "table": table},
                schema=[{"name": name, "type": str(t)} for name, t in resolved],
                row_count=row_count,
                source_version=None,
                load_mode=load_mode,
                time_partitioned=False,
                requested_dates=requested,
                actual_dates=[],
                notice=notice,
            )
            return {}

        # 时间分区表：分区即文件——每个 logical date 独立文件与独立分区身份，
        # 这是 Taosha Dataset（以及后续 #17 分区级缓存复用）的物理基础。
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

        schema = None
        total_rows = 0
        for index, date in enumerate(sorted(requested)):
            ctx.check_alive()
            file_name = build_hive_file_name(table, field, [date])
            row_count, resolved = self._export_one(
                ctx,
                build_partitioned_hive_sql(database, table, field, date),
                file_name,
                lowered,
                label=f"logical_date={date}",
            )
            if schema is None:
                schema = resolved
            elif [(n, str(t)) for n, t in resolved] != [(n, str(t)) for n, t in schema]:
                raise LoadJobError(
                    f"逻辑日期 {date} 的分区 schema 与首个分区不一致，拒绝合并为一个 Dataset"
                )
            # 文件描述符携带逻辑分区身份：Taosha 只消费 logical_date，
            # 不感知底层是 etl_date 还是 cdate。
            ctx.add_file(file_name, logical_date=date)
            total_rows += row_count
            logger.info(f"Hive load 分区完成 ({index + 1}/{len(requested)}): {table}__{date}")

        ctx.set_result(
            source={"type": "hive", "database": database, "table": table},
            schema=[{"name": name, "type": str(t)} for name, t in (schema or [])],
            row_count=total_rows,
            source_version=None,
            load_mode=load_mode,
            time_partitioned=True,
            requested_dates=requested,
            actual_dates=sorted(requested),
            notice=None,
        )
        return {}

    def _export_one(
        self,
        ctx,
        sql: str,
        file_name: str,
        declared_lower: dict,
        *,
        label: str,
    ):
        """导出一条 SQL 到一个 Parquet 文件（分批流式读，不整表进内存）。"""
        out_path = ctx.work_dir / file_name
        logger.info(f"Hive load 导出 ({label}): {sql}")
        with self._hive_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                names = [d[0] for d in cursor.description or []]
                return stream_to_parquet(
                    cursor.fetchmany,
                    [(n, declared_lower.get(str(n).lower())) for n in names],
                    out_path,
                    batch_rows=self._batch_rows(),
                    should_stop=ctx.check_alive,
                    declared_to_arrow=hive_arrow_type,
                )
            finally:
                cursor.close()

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
