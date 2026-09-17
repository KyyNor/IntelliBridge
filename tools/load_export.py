"""声明式 Load API：MySQL/Hive 整表或逻辑日期分区 → Parquet（issue #1）。

与现有 query 能力的分工（query 保持不变）：
    query = SQL / 小结果 / JSON（tools/mysql_query.py、tools/hive_query.py）
    load  = table + 逻辑日期 / 大数据抽取 / Parquet（本模块）

核心原则：Hive 只接受逻辑 dates[]。etl_date/cdate 物理分区字段由本模块根据
Hive metadata 自动适配，不向调用方暴露；两个候选字段并存时按平台配置
hive.primary_time_partition 选择主字段，不由调用方决定。
Taosha 侧业务规则（近一年任意日 / 历史仅自然月末）属于 Taosha #16，不在本层实现。

conditional load（issue #3）：调用方可带已知 source_version，本模块在**执行
SELECT 之前**比较当前版本，一致的逻辑日期不扫描数据、不生成 Parquet。
source_version 只用于 freshness 判断：读不到就退化为普通 load（status=unknown），
绝不因为读不到而返回 not_modified。
"""

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from utils.source_version import extract_source_version

import pymysql
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from utils.cache import cache
from utils.config import config
from utils.hive_errors import normalize_hive_error
from utils.hive_pool import hive_pool
from utils.load_jobs import (
    LoadJobCancelled,
    LoadJobError,
    LoadJobManager,
    LoadJobTimeout,
)
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

# 每个逻辑日期/表级的 conditional load 判定结果
STATUS_MODIFIED = "modified"
STATUS_NOT_MODIFIED = "not_modified"
STATUS_UNKNOWN = "unknown"

# load 自身的 DESCRIBE 串行化（与 hive_query 的 describe 缓存互不影响）
_describe_lock = threading.RLock()


class MySqlLoadRequest(BaseModel):
    database_id: str
    table: str


class HiveLoadRequest(BaseModel):
    database: str
    table: str
    dates: Optional[List[str]] = None
    # conditional load（issue #3）：已知版本，key 是逻辑日期，绝不出现物理分区字段
    known_versions: Optional[Dict[str, str]] = None
    # full_table 的表级已知版本（无时间分区表用这个，不用 known_versions）
    known_version: Optional[str] = None


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


def build_partition_describe_sql(
    database: str, table: str, partition_field: str, date: str
) -> str:
    """读单个分区元数据（source_version 探测）；只读 metadata，不触数据。"""
    return (
        f"DESCRIBE FORMATTED `{database}`.`{table}` "
        f"PARTITION (`{partition_field}`='{date}')"
    )


def build_table_describe_sql(database: str, table: str) -> str:
    """读表级元数据（full_table 的 source_version 探测）。"""
    return f"DESCRIBE FORMATTED `{database}`.`{table}`"


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


# ==================== 纯函数：conditional load 版本校验 ====================

def normalize_known_version(known_version) -> Optional[str]:
    """表级已知版本：空白视为未提供（= 不做比较，正常导出）。"""
    if known_version is None:
        return None
    text = str(known_version).strip()
    if not text:
        raise LoadJobError("known_version 为空，无法比较（不传即为普通 load）")
    return text


def normalize_known_versions(known_versions, requested_dates: List[str]) -> Dict[str, str]:
    """校验 known_versions 的日期 key 与版本值；只允许请求范围内的逻辑日期。"""
    if not known_versions:
        return {}
    if not isinstance(known_versions, dict):
        raise LoadJobError("known_versions 必须是 {logical_date: source_version} 对象")
    resolved: Dict[str, str] = {}
    for key, value in known_versions.items():
        date = normalize_requested_dates([key])[0]
        if date not in requested_dates:
            raise LoadJobError(
                f"known_versions 含未请求的日期 {date}；"
                f"只允许请求范围内的日期（dates={requested_dates}）"
            )
        if value is None or not str(value).strip():
            raise LoadJobError(f"known_versions[{date}] 版本为空，无法比较")
        resolved[date] = str(value).strip()
    return resolved


def resolve_version_status(known: Optional[str], current: Optional[str]) -> str:
    """单次比较的判定：一致才 not_modified；读不到当前版本一律 unknown（要导出）。

    方向性很关键——假 not_modified 会让调用方继续用陈旧数据，
    假 modified 只是多导出一次。因此只有「双方都有版本且相等」才返回 not_modified。
    """
    if known is None:
        return STATUS_MODIFIED  # 未要求比较：按普通 load 导出
    if current is None:
        return STATUS_UNKNOWN
    return STATUS_NOT_MODIFIED if current == known else STATUS_MODIFIED


def build_version_check(statuses: List[str], *, requested: bool) -> dict:
    """版本判定汇总（便于调用方与运维区分 fresh/refresh/不可比较）。"""
    return {
        "requested": requested,
        "not_modified": statuses.count(STATUS_NOT_MODIFIED),
        "modified": statuses.count(STATUS_MODIFIED),
        "unknown": statuses.count(STATUS_UNKNOWN),
    }


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

    def submit_hive(
        self,
        database: str,
        table: str,
        dates,
        known_versions=None,
        known_version=None,
    ) -> dict:
        if not IDENTIFIER_PATTERN.match(database or ""):
            return {"error": f"非法库名: {database!r}"}
        if not IDENTIFIER_PATTERN.match(table or ""):
            return {"error": f"非法表名: {table!r}"}
        try:
            requested = normalize_requested_dates(dates)
            # 版本入参在提交时就把形状校验掉：错到执行期只会浪费一次 job
            resolved_versions = normalize_known_versions(known_versions, requested)
            resolved_version = normalize_known_version(known_version)
        except LoadJobError as exc:
            return {"error": str(exc)}
        return self.manager.submit(
            "hive",
            {
                "database": database,
                "table": table,
                "requested_dates": requested,
                "known_versions": resolved_versions,
                "known_version": resolved_version,
            },
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

    # ---------- source_version（只读元数据） ----------

    def _read_partition_source_versions(
        self, database: str, table: str, field: str, dates: List[str]
    ) -> Dict[str, Optional[str]]:
        """逐分区读当前 source_version。

        读不到记 None（调用方退化为普通 load，绝不伪 not_modified）。
        分区不存在等单点异常后连接状态可能已不可用，剩余分区一并退化。
        """
        versions: Dict[str, Optional[str]] = {}
        if not dates:
            return versions
        try:
            with self._hive_connection() as conn:
                cursor = conn.cursor()
                try:
                    for date in dates:
                        try:
                            cursor.execute(
                                build_partition_describe_sql(database, table, field, date)
                            )
                            rows = cursor.fetchall()
                        except (LoadJobCancelled, LoadJobTimeout):
                            raise
                        except Exception as exc:
                            logger.warning(
                                f"Hive 分区 {database}.{table} logical_date={date} "
                                f"版本读取失败，退化为普通 load: {exc}"
                            )
                            break
                        version, _ = extract_source_version(rows, "partition")
                        versions[date] = version
                finally:
                    cursor.close()
        except (LoadJobCancelled, LoadJobTimeout):
            raise
        except Exception as exc:
            logger.warning(f"Hive 分区版本读取整体失败，退化为普通 load: {exc}")
        return {date: versions.get(date) for date in dates}

    def _read_table_source_version(self, database: str, table: str):
        """读表级 source_version；返回 (version, version_source)，读不到为 (None, None)。"""
        try:
            with self._hive_connection() as conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(build_table_describe_sql(database, table))
                    rows = cursor.fetchall()
                finally:
                    cursor.close()
        except (LoadJobCancelled, LoadJobTimeout):
            raise
        except Exception as exc:
            logger.warning(f"Hive 表 {database}.{table} 版本读取失败，退化为普通 load: {exc}")
            return None, None
        return extract_source_version(rows, "table")

    def _run_hive(self, ctx) -> dict:
        params = ctx.params
        database, table = params["database"], params["table"]
        requested = params.get("requested_dates") or []
        known_versions = params.get("known_versions") or {}
        known_version = params.get("known_version")
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
            if known_versions:
                raise LoadJobError(
                    f"表 {database}.{table} 没有时间分区（load_mode=full_table），"
                    "表级版本请用 known_version，不要传 known_versions"
                )
            load_mode = "full_table"
            notice = FULL_TABLE_NOTICE
            # 表级版本：条件比较只影响「要不要重新导出」，不改变全量语义
            current_version, version_source = self._read_table_source_version(database, table)
            status = resolve_version_status(known_version, current_version)
            if status == STATUS_NOT_MODIFIED:
                logger.info(f"Hive load 跳过导出（full_table 版本未变）: {database}.{table}")
                ctx.set_result(
                    source={"type": "hive", "database": database, "table": table},
                    schema=[],
                    row_count=0,
                    source_version=current_version,
                    version_source=version_source,
                    version_status=status,
                    load_mode=load_mode,
                    time_partitioned=False,
                    requested_dates=requested,
                    actual_dates=[],
                    notice=notice,
                    partitions=[],
                    version_check=build_version_check([status], requested=True),
                )
                return {}
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
                source_version=current_version,
                version_source=version_source,
                version_status=status,
                load_mode=load_mode,
                time_partitioned=False,
                requested_dates=requested,
                actual_dates=[],
                notice=notice,
                partitions=[],
                version_check=build_version_check([status], requested=known_version is not None),
            )
            return {}

        # 时间分区表：分区即文件——每个 logical date 独立文件与独立分区身份，
        # 这是 Taosha Dataset（以及后续 #17 分区级缓存复用）的物理基础。
        load_mode = "time_partitioned"
        if not requested:
            raise LoadJobError(
                f"表 {database}.{table} 是时间分区表，必须至少传一个逻辑日期（dates）"
            )
        if known_version is not None:
            raise LoadJobError(
                f"表 {database}.{table} 是时间分区表（load_mode=time_partitioned），"
                "分区版本请用 known_versions（按 logical_date），不要传表级 known_version"
            )
        available = self._show_partitions(database, table, field)
        missing = [d for d in requested if d not in available]
        if missing:
            preview = ", ".join(missing[:20]) + ("..." if len(missing) > 20 else "")
            raise LoadJobError(f"以下日期分区在表中不存在: {preview}")

        # 执行顺序（issue #3）：分区存在性 → 读当前版本 → 比较 → 只导出需要导出的
        current_versions = self._read_partition_source_versions(
            database, table, field, requested
        )
        partitions: List[dict] = []
        to_export: List[str] = []
        for date in requested:
            version = current_versions.get(date)
            status = resolve_version_status(known_versions.get(date), version)
            partitions.append(
                {
                    "logical_date": date,
                    "status": status,
                    "source_version": version,
                    "file_id": None,
                }
            )
            if status != STATUS_NOT_MODIFIED:
                to_export.append(date)

        schema = None
        total_rows = 0
        for index, date in enumerate(to_export):
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
            record = ctx.add_file(file_name, logical_date=date)
            for entry in partitions:
                if entry["logical_date"] == date:
                    entry["file_id"] = record["id"]
                    break
            total_rows += row_count
            logger.info(
                f"Hive load 分区完成 ({index + 1}/{len(to_export)}): {table}__{date}"
            )

        statuses = [entry["status"] for entry in partitions]
        ctx.set_result(
            source={"type": "hive", "database": database, "table": table},
            schema=[{"name": name, "type": str(t)} for name, t in (schema or [])],
            row_count=total_rows,
            source_version=None,  # 时间分区表的版本在 partitions[].source_version
            load_mode=load_mode,
            time_partitioned=True,
            requested_dates=requested,
            actual_dates=sorted(to_export),
            notice=None,
            partitions=partitions,
            version_check=build_version_check(
                statuses, requested=bool(known_versions)
            ),
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
    """提交 Hive 逻辑日期导出任务（Parquet），可选携带已知版本做 conditional load"""
    result = load_export.submit_hive(
        request.database,
        request.table,
        request.dates,
        known_versions=request.known_versions,
        known_version=request.known_version,
    )
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
