"""Turn verbose HiveServer2/Spark errors into bounded user-facing messages."""

import ast
import re
from typing import Dict, List, Tuple, Union


DEFAULT_MAX_HIVE_ERROR_LENGTH = 800
_ErrorInput = Union[str, BaseException]


def normalize_hive_error(
    error: _ErrorInput,
    max_length: int = DEFAULT_MAX_HIVE_ERROR_LENGTH,
) -> Dict[str, str]:
    """Extract and classify a Hive error without exposing its Java stack trace.

    PyHive commonly renders HiveServer2 failures as a ``TExecuteStatementResp``
    repr. The useful message is nested in its ``errorMessage`` field, while the
    surrounding value contains a repeated Java/Scala stack. This function only
    returns a small, bounded summary; callers can still log the original
    exception separately.
    """

    raw = str(error).strip()
    detail = _extract_error_message(raw) or raw
    category, message = _classify(detail)
    return {
        "category": category,
        "message": _truncate(message, max_length),
    }


def _extract_error_message(raw: str) -> str:
    """Read the nested ``errorMessage`` value from a Thrift response repr."""

    value = _extract_quoted_value(raw, r"errorMessage\s*=\s*")
    if value:
        return value
    return _extract_quoted_value(raw, r"infoMessages\s*=\s*\[\s*")


def _extract_quoted_value(raw: str, field_pattern: str) -> str:
    field_match = re.search(field_pattern + r"(?P<quote>[\"'])", raw, flags=re.DOTALL)
    if not field_match:
        return ""

    quote = field_match.group("quote")
    body_match = re.match(
        rf"(?P<body>(?:\\.|(?!{quote}).)*?){quote}",
        raw[field_match.end():],
        flags=re.DOTALL,
    )
    if not body_match:
        return ""

    body = body_match.group("body")
    try:
        return ast.literal_eval(f"{quote}{body}{quote}").strip()
    except (SyntaxError, ValueError):
        return (
            body.replace(r"\n", "\n")
            .replace(r"\r", "\r")
            .replace(r'\"', '"')
            .replace(r"\'", "'")
        ).strip()


def _classify(detail: str) -> Tuple[str, str]:
    """Map known Spark SQL diagnostics to concise summaries."""

    column_match = re.search(
        r"cannot resolve\s+['`\"]*([^'`\"\s]+)['`\"]*"
        r"\s+given input columns:\s*\[([^\]]*)\]",
        detail,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if column_match:
        column = column_match.group(1)
        available = _normalize_column_names(column_match.group(2))
        if available:
            return (
                "missing_column",
                f"列 `{column}` 不存在。可用列：{', '.join(available)}",
            )
        return "missing_column", f"列 `{column}` 不存在。"

    relation_match = re.search(
        r"Table or view not found:\s*([^;\s]+)",
        detail,
        flags=re.IGNORECASE,
    )
    if relation_match:
        relation = relation_match.group(1).strip("`'\"")
        return (
            "relation_not_found",
            f"表或视图 `{relation}` 不存在。"
            "请检查库名、表名和当前连接权限。",
        )

    if re.search(r"(?:^|\.)ParseException:", detail, flags=re.IGNORECASE):
        parser_detail = re.split(
            r"ParseException:\s*", detail, maxsplit=1, flags=re.IGNORECASE
        )[-1]
        parser_detail = _first_nonempty_line(parser_detail)
        return "syntax_error", f"SQL 语法错误：{parser_detail}"

    analysis_match = re.search(
        r"AnalysisException:\s*(.*)", detail, flags=re.IGNORECASE | re.DOTALL
    )
    if analysis_match:
        analysis_detail = _single_line(analysis_match.group(1))
        analysis_detail = re.split(
            r";\s*line\s+\d+\s+pos\s+\d+",
            analysis_detail,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        return "analysis_error", f"Hive 查询分析失败：{analysis_detail.strip()}"

    fallback = _single_line(detail)
    if not fallback:
        fallback = "未识别的 Hive 异常"
    return "unknown", f"Hive 查询失败：{fallback}"


def _normalize_column_names(raw_columns: str) -> List[str]:
    columns: List[str] = []
    for raw_column in raw_columns.split(","):
        column = raw_column.strip().strip("`'\"")
        if not column:
            continue
        column = column.rsplit(".", 1)[-1]
        if column not in columns:
            columns.append(column)
        if len(columns) == 12:
            break
    return columns


def _first_nonempty_line(value: str) -> str:
    for line in value.splitlines():
        line = line.strip()
        if line:
            return line
    return "未提供具体语法位置"


def _single_line(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _truncate(value: str, max_length: int) -> str:
    max_length = max(1, int(max_length))
    if len(value) <= max_length:
        return value
    if max_length == 1:
        return "…"
    return value[: max_length - 1].rstrip() + "…"
