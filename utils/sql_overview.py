"""SQL 结构概览：从 SQL 文本提取输入表、输出表、CTE 名称及行号。

给 ds_code_search 的降级路径用——当 pattern 为空或返回内容过大时，
用这个模块生成"任务结构概览"返回给 agent，代替原文，并提示 agent
用更精确的 pattern 重新查询。

设计要点：
- 多语句分别解析，按出现顺序返回，不判断语句间关系（不做 temp 表引用追踪）。
- 解析失败（脏 SQL / 占位符在奇怪位置）时该语句标记 parse_error，不阻断其他语句。
- CTE 行号相对整段原文定位（AST 拿别名 → 原文正则定位行号）。
- 依赖：sqlglot==25.27.0（已在 requirements.txt）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import sqlglot
from sqlglot import exp

DIALECT = "hive"


def summarize_sql(sql: str, dialect: str = DIALECT) -> Dict[str, Any]:
    """解析一段 SQL，返回结构化概览。

    一段 SQL 可能含多条语句（; 分隔），每条语句独立解析。

    Args:
        sql: 原始 SQL 文本。可为多语句。
        dialect: sqlglot 方言，默认 hive（spark 兼容）。

    Returns:
        {
          "parse_ok": bool,                # 整体能否解析（至少一条成功即 True）
          "statement_count": int,          # 语句条数
          "statements": [
            {
              "index": 0,                  # 语句序号（从 0 开始）
              "type": "Insert|Select|Create|...",  # 语句类型（sqlglot 节点类名）
              "input_tables": ["ods.x"],   # 直接来源表（FROM+JOIN），去重，排除 CTE 引用
              "output_table": "dws.y",     # INSERT/CREATE 目标表，无则 None
              "output_kind": "INSERT|CREATE|None",  # 写入类型
              "ctes": [                     # 本语句定义的 CTE（含嵌套）
                {"name": "cte_a", "line": 3},  # line 是相对整段原文的起始行号
              ],
              "parse_error": None,         # 失败时填错误信息，成功为 None
            }, ...
          ]
        }
    """
    if not sql or not sql.strip():
        return {"parse_ok": False, "statement_count": 0, "statements": []}

    # 第一步：切多语句。整体失败则返回单条 parse_error，但尝试逐条挽救。
    parsed_list = _safe_parse_all(sql, dialect)
    if parsed_list is None:
        # 整体切分失败：作为单条记录标记失败
        return {
            "parse_ok": False,
            "statement_count": 1,
            "statements": [{
                "index": 0,
                "type": "Unknown",
                "input_tables": [],
                "output_table": None,
                "output_kind": None,
                "ctes": [],
                "parse_error": "整体解析失败（可能是语法错误或占位符干扰）",
            }],
        }

    statements = []
    any_ok = False
    for idx, stmt in enumerate(parsed_list):
        if stmt is None:
            statements.append(_empty_stmt(idx, "空语句（None）"))
            continue
        try:
            info = _describe_statement(stmt, sql, dialect)
            info["index"] = idx
            info["parse_error"] = None
            statements.append(info)
            any_ok = True
        except Exception as e:  # noqa: BLE001 单条失败不能拖垮其他
            statements.append(_empty_stmt(idx, f"{type(e).__name__}: {e}"))

    return {
        "parse_ok": any_ok,
        "statement_count": len(statements),
        "statements": statements,
    }


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------

def _safe_parse_all(sql: str, dialect: str) -> Optional[List[Optional[exp.Expression]]]:
    """sqlglot.parse 包装。失败返回 None。"""
    try:
        return sqlglot.parse(sql, dialect=dialect)
    except Exception:  # noqa: BLE001
        return None


def _empty_stmt(idx: int, error: str) -> Dict[str, Any]:
    """构造一个解析失败的语句记录。"""
    return {
        "index": idx,
        "type": "Unknown",
        "input_tables": [],
        "output_table": None,
        "output_kind": None,
        "ctes": [],
        "parse_error": error,
    }


def _describe_statement(
    stmt: exp.Expression, full_sql: str, dialect: str
) -> Dict[str, Any]:
    """提取单条语句的结构信息。"""
    # 语句类型
    stmt_type = type(stmt).__name__

    # 输出表（INSERT / CREATE）
    output_table, output_kind = _extract_output(stmt, dialect)

    # 输入表 + CTE 名集合（CTE 名用于从输入表里排除 CTE 引用）
    cte_names = {cte.alias for cte in stmt.find_all(exp.CTE) if cte.alias}
    input_tables = _extract_input_tables(stmt, cte_names, dialect)

    # CTE 名称 + 行号
    ctes = _extract_ctes_with_lines(stmt, full_sql)

    return {
        "type": stmt_type,
        "input_tables": input_tables,
        "output_table": output_table,
        "output_kind": output_kind,
        "ctes": ctes,
    }


def _extract_output(
    stmt: exp.Expression, dialect: str
) -> tuple[Optional[str], Optional[str]]:
    """提取写入目标表。返回 (表名, 类型)。

    INSERT INTO/OVERWRITE -> (target, "INSERT")
    CREATE [TEMPORARY] TABLE/VIEW ... AS -> (target, "CREATE")
    纯 SELECT -> (None, None)
    """
    ins = stmt.find(exp.Insert)
    if ins and ins.this:
        # ins.this 可能是 Table 或 Schema（带列定义）；Table 节点的 this 才是表名
        tgt = ins.this
        if isinstance(tgt, exp.Table):
            return tgt.sql(dialect=dialect), "INSERT"
        # Schema: ins.this.sql() 会带列，这里仍返回完整串
        return tgt.sql(dialect=dialect), "INSERT"

    create = stmt.find(exp.Create)
    if create and create.this:
        tgt = create.this
        if isinstance(tgt, exp.Table):
            return tgt.sql(dialect=dialect), "CREATE"
        return tgt.sql(dialect=dialect), "CREATE"

    return None, None


def _extract_input_tables(
    stmt: exp.Expression, cte_names: set[str], dialect: str
) -> List[str]:
    """提取直接来源表（FROM + JOIN），去重，排除 CTE 引用。

    关键：只用 select.args['from'] 和 select.args['joins']，
    不用 find_all(Table)，否则会把子查询/CTE 体内的表全抓出来。
    """
    seen: List[str] = []
    for sel in stmt.find_all(exp.Select):
        # FROM
        from_node = sel.args.get("from")
        if from_node and from_node.this:
            _collect_source(from_node.this, cte_names, dialect, seen)
        # JOINs（无 JOIN 时为 None）
        for join in sel.args.get("joins", []) or []:
            if join.this:
                _collect_source(join.this, cte_names, dialect, seen)
    return seen


def _collect_source(
    node: exp.Expression, cte_names: set[str], dialect: str, seen: List[str]
) -> None:
    """把一个 FROM/JOIN 源节点压进 seen（去重，排除 CTE 引用）。

    node 可能是 Table（普通表）、Subquery（派生表，取其内部第一个表）、或别名包裹的两者。
    """
    # unwrap 别名：node 可能是 Alias，this 才是真正的表
    inner = node
    while hasattr(inner, "this") and not isinstance(inner, (exp.Table, exp.Subquery)):
        inner = inner.this

    if isinstance(inner, exp.Table):
        # 排除 CTE 引用（裸名命中 CTE 名集合）
        if inner.name in cte_names:
            return
        # 用 db.table 形式，去掉表别名（Table.sql() 会带 " AS alias"）
        name = inner.sql(dialect=dialect).split(" AS ")[0].strip()
        if name not in seen:
            seen.append(name)
    elif isinstance(inner, exp.Subquery):
        # 派生表：取子查询内部的第一个表（best-effort，不递归太深）
        first_tbl = inner.find(exp.Table)
        if first_tbl and first_tbl.name not in cte_names:
            name = first_tbl.sql(dialect=dialect)
            if name not in seen:
                seen.append(name)


def _extract_ctes_with_lines(
    stmt: exp.Expression, full_sql: str
) -> List[Dict[str, Any]]:
    """提取本语句所有 CTE（含嵌套）的名称 + 起始行号。

    行号相对整段原文：用「AST 别名 → 原文正则定位」法。
    之所以不用 sqlglot 的 node.meta：它默认不保留源码位置（实验验证为空）。
    """
    out: List[Dict[str, Any]] = []
    for cte in stmt.find_all(exp.CTE):
        alias = cte.alias
        if not alias:
            continue
        line = _locate_cte_line(alias, full_sql)
        out.append({"name": alias, "line": line})
    return out


def _locate_cte_line(alias: str, full_sql: str) -> Optional[int]:
    """在原文里定位 CTE 定义起始行号。

    匹配 `别名 AS (` 或 `别名 AS(`（忽略大小写、容忍多空格）。
    返回 1-based 行号；定位不到返回 None。
    """
    pat = rf"\b{re.escape(alias)}\s+AS\s*\("
    m = re.search(pat, full_sql, re.IGNORECASE)
    if not m:
        return None
    return full_sql[: m.start()].count("\n") + 1
