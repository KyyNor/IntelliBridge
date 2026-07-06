import re
from typing import List, Optional


def remove_comments(sql: str) -> str:
    """
    移除 SQL 中的注释

    支持:
    - 单行注释: -- 注释内容
    - 多行注释: /* 注释内容 */

    Args:
        sql: 原始 SQL 语句

    Returns:
        移除注释后的 SQL 语句
    """
    # 先移除多行注释 /* ... */
    sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.DOTALL)

    # 再移除单行注释 -- ...
    lines = sql.split('\n')
    filtered_lines = []
    for line in lines:
        # 检测并移除 -- 注释（注意 -- 可能出现在字符串里，这里简单处理）
        stripped = line.strip()
        if stripped.startswith('--'):
            continue
        filtered_lines.append(line)
    return '\n'.join(filtered_lines)


def check_sql_type(
    sql: str,
    allowed_prefixes: Optional[List[str]] = None,
    forbidden_keywords: Optional[List[str]] = None
) -> str:
    """
    检查 SQL 类型是否允许执行

    Args:
        sql: SQL 语句（已移除注释）
        allowed_prefixes: 允许的开头关键字列表，如 ['SELECT', 'WITH']
        forbidden_keywords: 禁止的关键字列表，如 ['INSERT', 'DELETE', 'DROP']

    Returns:
        "ok" 表示通过，否则返回错误信息
    """
    if allowed_prefixes is None:
        allowed_prefixes = ['SELECT']

    if forbidden_keywords is None:
        forbidden_keywords = ['INSERT', 'DELETE', 'DROP']

    sql_stripped = sql.strip()

    # 检查是否包含禁止的关键字
    for keyword in forbidden_keywords:
        if re.search(rf'\b{keyword}\b', sql_stripped, re.IGNORECASE):
            return f"错误: 不允许执行 {keyword} 操作"

    # 检查是否以允许的关键词开头
    prefixes_pattern = '|'.join(allowed_prefixes)
    if not re.match(rf'^\s*({prefixes_pattern})\s', sql_stripped, re.IGNORECASE):
        prefixes_str = '、'.join(allowed_prefixes)
        return f"错误: 只允许执行 {prefixes_str} 查询"

    return "ok"