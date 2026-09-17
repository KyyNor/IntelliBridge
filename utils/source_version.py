"""Hive source_version 解析（issue #3）：只读元数据，不扫描数据、不做文件 hash。

source_version 只用于 freshness 判断——Taosha 侧把它当 opaque string 比较，
不解释格式、不用于授权，也不作为物理分区身份。

解析对象是 Hive/Spark `DESCRIBE FORMATTED` 的输出：

- 分区级：`DESCRIBE FORMATTED db.t PARTITION (p='...')` 的 Partition Parameters
- 表级：`DESCRIBE FORMATTED db.t` 的 Table Parameters / Table Properties

两种真实输出形状都要兼容：

    Spark:  ('Partition Parameters', '{transient_lastDdlTime=..., numFiles=2}', '')
    Hive:   ('Table Parameters:', '', '') + ('transient_lastDdlTime', '1789615800', '')

读不到就是读不到：返回 None，由调用方退化为普通 load（绝不伪造成 not_modified）。
"""

import re
from typing import Dict, Optional, Tuple

VERSION_KEY = "transient_lastDdlTime"

# 段落标记 → 规范名。两种服务端输出都用冒号结尾的段头或带内联 map 的键值行。
_PARTITION_PARAMS = "partition_parameters"
_TABLE_PARAMS = "table_parameters"
_TABLE_PROPERTIES = "table_properties"

_SECTION_BY_MARKER = {
    "partition parameters": _PARTITION_PARAMS,
    "partition properties": _PARTITION_PARAMS,
    "table parameters": _TABLE_PARAMS,
    "table properties": _TABLE_PROPERTIES,
}

# 分区级版本只能来自分区自身的参数：表级参数对所有分区相同，
# 用它当分区版本会让所有分区一起“看起来没变”（危险方向），因此严格隔离。
_SECTION_ORDER = {
    "partition": (_PARTITION_PARAMS,),
    "table": (_TABLE_PARAMS, _TABLE_PROPERTIES),
}

_INLINE_MAP_PATTERN = re.compile(r"^\s*[\[{](.*)[\]}]\s*$", re.DOTALL)


def _cell(row, index: int) -> str:
    try:
        value = row[index]
    except (IndexError, TypeError):
        return ""
    return "" if value is None else str(value)


def parse_inline_map(text: str) -> Dict[str, str]:
    """解析 `{k=v, k=v}` / `[k=v, k=v]` 形式的内联参数 map。

    值里可能含 `:` `.` `/`（如 path=file:/tmp/x）；用第一个 `=` 切分，
    保留值原样（不做类型转换）。
    """
    match = _INLINE_MAP_PATTERN.match(text or "")
    if not match:
        return {}
    body = match.group(1).strip()
    if not body:
        return {}
    parsed: Dict[str, str] = {}
    for chunk in body.split(","):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            parsed.setdefault(key, value)
    return parsed


def parse_describe_formatted(rows) -> Dict[str, Dict[str, str]]:
    """解析 DESCRIBE FORMATTED 输出 → {段落规范名: {参数名: 值}}。

    只在明确的参数段落里收集键值：段头行（`# Storage Information` 等）
    与空行都结束当前段落，避免把后续 `Location`/`Created Time` 混进参数。
    """
    sections: Dict[str, Dict[str, str]] = {}
    current: Optional[Dict[str, str]] = None
    for row in rows or []:
        name = _cell(row, 0).strip()
        value = _cell(row, 1).strip() or _cell(row, 2).strip()
        marker = name.rstrip(":").strip().lower()
        section = _SECTION_BY_MARKER.get(marker)
        if section is not None:
            bucket = sections.setdefault(section, {})
            inline = parse_inline_map(value)
            if inline:
                bucket.update(inline)
                current = None  # Spark：参数在同一行的内联 map 里，段落到此结束
            else:
                current = bucket  # Hive：段头行之后逐行给出 key/value
            continue
        if not name or name.startswith("#"):
            current = None
            continue
        if current is not None and value:
            current.setdefault(name, value)
    return sections


def normalize_version(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def extract_source_version(rows, mode: str) -> Tuple[Optional[str], Optional[str]]:
    """从 DESCRIBE FORMATTED 输出提取 (source_version, version_source)。

    mode='partition' 只认分区参数；mode='table' 认表级参数/属性。
    两者都读不到时返回 (None, None)——调用方必须退化为普通 load。
    """
    if mode not in _SECTION_ORDER:
        raise ValueError(f"未知的 source_version 模式: {mode!r}")
    sections = parse_describe_formatted(rows)
    for section in _SECTION_ORDER[mode]:
        version = normalize_version((sections.get(section) or {}).get(VERSION_KEY))
        if version:
            return version, section
    return None, None
