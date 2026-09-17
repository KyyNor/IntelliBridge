"""DB 游标 → Parquet 的流式导出（PyArrow）。

全链路分批读取（fetchmany），只在批次内转列写 ParquetWriter：
绝不把整表装进内存，也绝不经过 JSON（区别于现有 query 的 rows 链路）。
"""

import datetime
import decimal
import hashlib
import json
import re
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import pyarrow as pa
import pyarrow.parquet as pq

DEFAULT_BATCH_ROWS = 20000
_READ_CHUNK = 1 << 20

_DECIMAL_PATTERN = re.compile(r"^decimal\(\s*(\d+)\s*,\s*(\d+)\s*\)$")


class ParquetExportError(Exception):
    """导出/类型转换失败（含列名上下文）。"""


# ==================== 声明类型 → Arrow 类型 ====================

def hive_arrow_type(declared: str) -> pa.DataType:
    """Hive/Spark 声明类型 → Arrow 类型；未识别类型退化为 string。"""
    text = (declared or "").strip().lower()
    match = _DECIMAL_PATTERN.match(text)
    if match:
        precision, scale = int(match.group(1)), int(match.group(2))
        return pa.decimal128(min(max(precision, 1), 38), min(scale, 38))
    family = text.split("(", 1)[0].strip()
    mapping = {
        "boolean": pa.bool_(),
        "tinyint": pa.int8(),
        "smallint": pa.int16(),
        "int": pa.int32(),
        "bigint": pa.int64(),
        "float": pa.float32(),
        "double": pa.float64(),
        "date": pa.date32(),
        "timestamp": pa.timestamp("us"),
        "string": pa.string(),
        "varchar": pa.string(),
        "char": pa.string(),
        "binary": pa.binary(),
    }
    return mapping.get(family, pa.string())


def mysql_arrow_type(column_type: str) -> pa.DataType:
    """MySQL information_schema COLUMN_TYPE → Arrow 类型。"""
    text = (column_type or "").strip().lower()
    match = _DECIMAL_PATTERN.match(text)
    if match:
        precision, scale = int(match.group(1)), int(match.group(2))
        return pa.decimal128(min(max(precision, 1), 38), min(scale, 38))
    tokens = text.replace("(", " (").split()
    if not tokens:
        return pa.string()
    base, unsigned = tokens[0], "unsigned" in tokens
    if base in ("tinyint", "bool", "boolean"):
        return pa.int16() if unsigned else pa.int8()
    if base == "smallint":
        return pa.int32() if unsigned else pa.int16()
    if base in ("mediumint", "middleint"):
        return pa.int32()
    if base in ("int", "integer"):
        return pa.int64() if unsigned else pa.int32()
    if base == "bigint" or base == "serial":
        return pa.uint64() if unsigned else pa.int64()
    if base in ("float", "real"):
        return pa.float32()
    if base == "double" or base.startswith("double"):
        return pa.float64()
    if base == "year":
        return pa.int16()
    if base == "date":
        return pa.date32()
    if base in ("datetime", "timestamp"):
        return pa.timestamp("us")
    if base == "time":
        return pa.time64("us")
    if base in ("bit",) or base in ("binary", "varbinary") or base.endswith("blob"):
        return pa.binary()
    return pa.string()


ArrowFactory = Callable[[str], pa.DataType]


# ==================== 值转换 ====================

def _parse_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value).strip()
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"无法解析日期: {value!r}") from exc


def _parse_datetime(value):
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    text = str(value).strip().replace(" ", "T", 1)
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间戳: {value!r}") from exc
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _parse_time(value):
    if isinstance(value, datetime.timedelta):
        total = int(value.total_seconds())
        micros = value.microseconds
        return datetime.time(total // 3600, (total % 3600) // 60, total % 60, micros)
    if isinstance(value, datetime.time):
        return value
    text = str(value).strip()
    try:
        return datetime.time.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {value!r}") from exc


def convert_value(value, arrow_type: pa.DataType):
    """按目标 Arrow 类型归一化单个值（None 原样保留）。"""
    if value is None:
        return None
    t = arrow_type
    if pa.types.is_string(t):
        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, (datetime.datetime, datetime.date)):
            return value.isoformat()
        return str(value)
    if pa.types.is_boolean(t):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        raise ValueError(f"无法解析布尔值: {value!r}")
    if pa.types.is_integer(t):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, decimal.Decimal):
            return int(value)
        return int(str(value).strip())
    if pa.types.is_floating(t):
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, decimal.Decimal):
            return float(value)
        return float(str(value).strip())
    if pa.types.is_decimal(t):
        if isinstance(value, decimal.Decimal):
            return value
        if isinstance(value, bool):
            return decimal.Decimal(int(value))
        if isinstance(value, int):
            return decimal.Decimal(value)
        return decimal.Decimal(str(value).strip())
    if pa.types.is_date(t):
        return _parse_date(value)
    if pa.types.is_timestamp(t):
        return _parse_datetime(value)
    if pa.types.is_time(t):
        return _parse_time(value)
    if pa.types.is_binary(t):
        if isinstance(value, bytes):
            return value
        if isinstance(value, memoryview):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        return bytes(value)
    return value


def infer_arrow_type(values: Sequence) -> pa.DataType:
    """按首批非空值推断 Arrow 类型（仅用于缺少声明类型的列）。"""
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return pa.bool_()
        if isinstance(value, int):
            return pa.int64()
        if isinstance(value, float):
            return pa.float64()
        if isinstance(value, decimal.Decimal):
            scale = max(-value.as_tuple().exponent, 0)
            precision = max(len(value.as_tuple().digits), scale, 1)
            return pa.decimal128(min(precision, 38), scale)
        if isinstance(value, datetime.datetime):
            return pa.timestamp("us")
        if isinstance(value, datetime.date):
            return pa.date32()
        if isinstance(value, bytes):
            return pa.binary()
        return pa.string()
    return pa.string()


# ==================== 批次与流式写 ====================

def _resolve_columns(
    columns_meta: Sequence[Tuple[str, Optional[str]]],
    first_rows: Sequence,
    row_to_values: Callable[[object], Sequence],
    declared_to_arrow: Optional[ArrowFactory],
) -> List[Tuple[str, pa.DataType]]:
    """列名 + 声明类型 → Arrow 列类型；声明缺失时按首批值推断。"""
    if not columns_meta and first_rows:
        row_values = row_to_values(first_rows[0])
        columns_meta = [(f"c{i + 1}", None) for i in range(len(row_values))]
    resolved = []
    for index, (name, declared) in enumerate(columns_meta):
        arrow_type = None
        if declared and declared_to_arrow is not None:
            arrow_type = declared_to_arrow(declared)
        if arrow_type is None or pa.types.is_string(arrow_type) and declared is None:
            values = [row_to_values(row)[index] for row in first_rows]
            arrow_type = infer_arrow_type(values)
        resolved.append((name, arrow_type))
    return resolved


def _check_stop(should_stop: Optional[Callable[[], None]]) -> None:
    if should_stop is not None:
        should_stop()


def stream_to_parquet(
    fetch_batch: Callable[[int], List],
    columns_meta: Sequence[Tuple[str, Optional[str]]],
    out_path,
    *,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    should_stop: Optional[Callable[[], None]] = None,
    row_to_values: Callable[[object], Sequence] = list,
    declared_to_arrow: Optional[ArrowFactory] = None,
) -> Tuple[int, List[Tuple[str, pa.DataType]]]:
    """分批拉取并流式写 Parquet，返回 (row_count, 解析后的列类型)。

    fetch_batch(n) 必须像 cursor.fetchmany 一样最多返回 n 行；
    0 行也会产出带 schema 的合法 Parquet 文件。
    """
    out_path = Path(out_path)
    writer = None
    resolved: Optional[List[Tuple[str, pa.DataType]]] = None
    total = 0
    try:
        while True:
            _check_stop(should_stop)
            rows = fetch_batch(batch_rows) or []
            if not rows:
                break
            if resolved is None:
                resolved = _resolve_columns(columns_meta, rows, row_to_values, declared_to_arrow)
                writer = pq.ParquetWriter(
                    out_path,
                    pa.schema([pa.field(name, t, nullable=True) for name, t in resolved]),
                    compression="snappy",
                )
            writer.write_table(_convert_batch(rows, resolved, row_to_values))
            total += len(rows)
        if writer is None:
            resolved = _resolve_columns(columns_meta, [], row_to_values, declared_to_arrow)
            writer = pq.ParquetWriter(
                out_path,
                pa.schema([pa.field(name, t, nullable=True) for name, t in resolved]),
                compression="snappy",
            )
    except BaseException:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            writer = None
        raise
    finally:
        if writer is not None:
            writer.close()
    return total, resolved


def _convert_batch(
    rows: Sequence,
    resolved: Sequence[Tuple[str, pa.DataType]],
    row_to_values: Callable[[object], Sequence],
) -> pa.Table:
    materialized = [row_to_values(row) for row in rows]
    arrays = []
    for index, (name, arrow_type) in enumerate(resolved):
        try:
            values = [convert_value(row[index], arrow_type) for row in materialized]
            arrays.append(pa.array(values, type=arrow_type))
        except (ValueError, TypeError, ArithmeticError, pa.ArrowException) as exc:
            raise ParquetExportError(f"列 {name} 转换失败（{arrow_type}）: {exc}") from exc
    return pa.Table.from_arrays(
        arrays, schema=pa.schema([pa.field(name, t, nullable=True) for name, t in resolved])
    )


def file_digest(path) -> Tuple[int, str]:
    """流式计算文件 (size, sha256)。"""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_READ_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()
