"""DolphinScheduler 代码检索工具"""

import regex
import json
from typing import Optional, List, Dict, Any

from utils.mysql_pool import mysql_pool
from utils.logger import logger
from utils.decorators import log_function_info
from utils.mcp import mcp


class DataFactoryCodeSearch:
    """DolphinScheduler 代码检索工具类"""

    MAX_LIMIT = 1000
    DEFAULT_PAGE_SIZE = 50
    REGEX_TIMEOUT = 5  # 秒

    def __init__(self):
        pass

    # ========== Task 2: 路径解析辅助方法 ==========

    def parse_code_path(self, code_path: str) -> Dict[str, Optional[str]]:
        """
        解析 code_path 为项目、工作流、任务三层结构

        Args:
            code_path: 路径字符串，如 /ds/ODS/日报工作流/用户表同步

        Returns:
            包含 project, process, task 的字典
        """
        # 移除首尾空格
        code_path = code_path.strip().rstrip('/')

        if not code_path or code_path == '/ds':
            return {"project": None, "process": None, "task": None}

        # 去掉前缀 /ds/
        if code_path.startswith('/ds'):
            code_path = code_path[3:].lstrip('/')

        parts = code_path.split('/')
        return {
            "project": parts[0] if len(parts) > 0 else None,
            "process": parts[1] if len(parts) > 1 else None,
            "task": parts[2] if len(parts) > 2 else None
        }

    def build_like_pattern(self, code_path: str) -> str:
        """
        将 code_path 转换为 SQL LIKE 模式

        Args:
            code_path: 原始路径

        Returns:
            LIKE 模式字符串，如 %ODS%
        """
        parsed = self.parse_code_path(code_path)
        pattern_parts = []

        if parsed["project"]:
            pattern_parts.append(parsed["project"])
        if parsed["process"]:
            pattern_parts.append(parsed["process"])
        if parsed["task"]:
            pattern_parts.append(parsed["task"])

        # 拼接成 %keyword% 形式
        return '%'.join(pattern_parts) if pattern_parts else '%'

    # ========== Task 3: 任务状态判定方法 ==========

    def determine_task_status(self, process_status: Optional[str], task_status: Optional[str]) -> str:
        """
        判定任务最终状态

        规则: process_status=1 且 task_status=1 = 已上线，其他均为未上线

        Args:
            process_status: 工作流状态
            task_status: 任务状态

        Returns:
            已上线/未上线
        """
        if process_status == "1" and task_status == "1":
            return "已上线"
        return "未上线"

    # ========== Task 4: SQL 代码搜索核心功能 ==========

    def search_sql_codes(
        self,
        pattern: str,
        code_path: str = "/ds/",
        before: int = 0,
        after: int = 0,
        code_status: str = "已上线",
        page: int = 1,
        page_size: int = 50
    ) -> Dict[str, Any]:
        """
        搜索 SQL 代码

        Args:
            pattern: 正则表达式
            code_path: 路径过滤
            before: 匹配行前的行数
            after: 匹配行后的行数
            code_status: 状态过滤
            page: 页码
            page_size: 每页条数

        Returns:
            包含 matches 和 pagination 的字典
        """
        logger.info(f"开始搜索: pattern={pattern}, code_path={code_path}, code_status={code_status}")

        # 1. 解析 code_path
        parsed = self.parse_code_path(code_path)

        # 2. 确定状态过滤条件
        status_filter = None
        if code_status == "已上线":
            status_filter = {"process_status": "1", "task_status": "1"}
        elif code_status == "未上线":
            status_filter = {"process_status": "0"}

        # 3. 构建查询 SQL
        sql = """
            SELECT
                project_name,
                process_name,
                task_name,
                process_status,
                task_status,
                sql_code,
                lineage_type
            FROM metadata_ds_table_lineage
            WHERE sql_code IS NOT NULL AND sql_code != ''
        """

        params = []

        # 添加项目过滤
        if parsed["project"]:
            sql += " AND project_name LIKE %s"
            params.append(f"%{parsed['project']}%")

        # 添加工作流过滤
        if parsed["process"]:
            sql += " AND process_name LIKE %s"
            params.append(f"%{parsed['process']}%")

        # 添加任务过滤
        if parsed["task"]:
            sql += " AND task_name LIKE %s"
            params.append(f"%{parsed['task']}%")

        # 添加状态过滤
        if status_filter:
            if code_status == "已上线":
                sql += " AND process_status = %s AND task_status = %s"
                params.extend([status_filter["process_status"], status_filter["task_status"]])
            elif code_status == "未上线":
                sql += " AND (process_status = '0' OR task_status = '0')"

        # 4. 执行查询
        try:
            with mysql_pool.get_connection("local_node_data_factory") as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                results = cursor.fetchall()
            logger.info(f"数据库查询完成，共获取 {len(results)} 条任务")
        except Exception as e:
            logger.error(f"查询失败: {e}")
            return {"matches": [], "pagination": {}, "error": str(e)}

        # 5. 内存正则匹配
        matches = []

        # 先预编译正则表达式以提升性能
        try:
            compiled_regex = regex.compile(pattern)
        except regex.error as e:
            logger.error(f"正则表达式编译失败: {e}")
            return {"matches": [], "pagination": {}, "error": f"正则表达式错误: {e}"}

        for row in results:
            sql_code = row.get('sql_code', '')
            if not sql_code:
                continue

            # 逐行匹配
            lines = sql_code.split('\n')
            for line_num, line in enumerate(lines, start=1):
                try:
                    # 直接用编译后的正则搜索，配合 timeout
                    match = compiled_regex.search(line)
                    if match:
                        # 提取上下文
                        start_idx = max(0, line_num - 1 - before)
                        end_idx = min(len(lines), line_num - 1 + after + 1)
                        context_lines = lines[start_idx:end_idx]

                        matches.append({
                            "code_path": f"/ds/{row['project_name']}/{row['process_name']}/{row['task_name']}",
                            "行号": line_num,
                            "代码片段": '\n'.join(context_lines),
                            "任务状态": self.determine_task_status(
                                str(row.get('process_status')),
                                str(row.get('task_status'))
                            )
                        })
                except Exception as e:
                    # 捕获潜在的正则匹配异常，记录日志但继续处理
                    logger.warning(f"正则匹配异常: {row['task_name']}, 行 {line_num}, 错误: {e}")

        logger.info(f"正则匹配完成，共找到 {len(matches)} 处匹配")

        # 6. 分页处理
        total = len(matches)
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size

        paginated_matches = matches[start_idx:end_idx]

        return {
            "matches": paginated_matches,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages
            }
        }

    # ========== Task 5: 任务详情查询核心功能 ==========

    def query_task_info(
        self,
        code_path: Optional[str] = None,
        from_table: Optional[str] = None,
        to_table: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        查询任务详情

        Args:
            code_path: 路径筛选，支持精确或模糊匹配
            from_table: 输入表名称筛选，支持模糊匹配
            to_table: 输出表名称筛选，支持模糊匹配

        Returns:
            任务详情列表
        """
        # 参数校验：至少需要一个筛选条件
        if not code_path and not from_table and not to_table:
            return [{"error": "请至少填写一个筛选条件"}]

        logger.info(f"查询任务详情: code_path={code_path}, from_table={from_table}, to_table={to_table}")

        # 构建查询
        sql = """
            SELECT
                project_name,
                process_name,
                task_name,
                process_status,
                task_status,
                from_database_table,
                to_database_table,
                sql_code,
                lineage_type
            FROM metadata_ds_table_lineage
            WHERE 1=1
        """

        params = []

        if code_path:
            parsed = self.parse_code_path(code_path)
            if parsed["project"]:
                sql += " AND project_name LIKE %s"
                params.append(f"%{parsed['project']}%")
            if parsed["process"]:
                sql += " AND process_name LIKE %s"
                params.append(f"%{parsed['process']}%")
            if parsed["task"]:
                sql += " AND task_name LIKE %s"
                params.append(f"%{parsed['task']}%")

        if from_table:
            sql += " AND from_database_table LIKE %s"
            params.append(f"%{from_table}%")

        if to_table:
            sql += " AND to_database_table LIKE %s"
            params.append(f"%{to_table}%")

        # 执行查询
        try:
            with mysql_pool.get_connection("local_node_data_factory") as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params)
                results = cursor.fetchall()
            logger.info(f"查询完成，共获取 {len(results)} 条任务")
        except Exception as e:
            logger.error(f"查询失败: {e}")
            return [{"error": str(e)}]

        # 处理结果
        tasks = []
        for row in results:
            # 解析表清单
            from_tables = []
            if row.get('from_database_table'):
                from_tables = [t.strip() for t in row['from_database_table'].split(',') if t.strip()]

            to_tables = []
            if row.get('to_database_table'):
                to_tables = [t.strip() for t in row['to_database_table'].split(',') if t.strip()]

            # 截取前100行
            sql_code = row.get('sql_code', '') or ''
            code_lines = sql_code.split('\n')
            code_preview = '\n'.join(code_lines[:100])
            total_lines = len(code_lines)

            task_status = self.determine_task_status(
                str(row.get('process_status')),
                str(row.get('task_status'))
            )

            tasks.append({
                "code_path": f"/ds/{row['project_name']}/{row['process_name']}/{row['task_name']}",
                "任务类型": row.get('lineage_type', 'SQL'),
                "代码": code_preview,
                "代码行数": total_lines,
                "任务状态": task_status,
                "输入表清单": from_tables,
                "输出表清单": to_tables
            })

        return tasks


# 默认实例
ds_code_search = DataFactoryCodeSearch()


# ==================== MCP 工具 ====================

@mcp.tool()
@log_function_info
def datafactory_sql_search(
    pattern: str,
    code_path: str = "/ds/",
    before: int = 0,
    after: int = 0,
    code_status: str = "已上线",
    page: int = 1,
    page_size: int = 50
) -> str:
    """
    DolphinScheduler 代码检索工具（类 grep）

    对标 grep 的代码检索工具，支持正则匹配、路径过滤、上下文参数、状态过滤和分页。

    Args:
        pattern: 正则表达式
        code_path: 路径过滤，支持部分匹配（如 /ds/ODS/）
        before: 匹配行前的行数
        after: 匹配行后的行数
        code_status: 状态过滤：已上线/未上线/全部
        page: 页码
        page_size: 每页条数

    Returns:
        JSON 格式的搜索结果
    """
    result = ds_code_search.search_sql_codes(
        pattern=pattern,
        code_path=code_path,
        before=before,
        after=after,
        code_status=code_status,
        page=page,
        page_size=page_size
    )

    if "error" in result:
        return f"错误: {result['error']}"

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
@log_function_info
def datafactory_task_info(
    code_path: Optional[str] = None,
    from_table: Optional[str] = None,
    to_table: Optional[str] = None
) -> str:
    """
    DolphinScheduler 任务详情查询工具

    通过 code_path、输入表或输出表筛选任务，返回任务详情信息。

    Args:
        code_path: 路径筛选，支持精确或模糊匹配（如 /ds/*/*/决策）
        from_table: 输入表名称筛选，支持模糊匹配
        to_table: 输出表名称筛选，支持模糊匹配

    Returns:
        JSON 格式的任务详情列表
    """
    result = ds_code_search.query_task_info(
        code_path=code_path,
        from_table=from_table,
        to_table=to_table
    )

    if result and isinstance(result, list) and "error" in result[0]:
        return f"错误: {result[0]['error']}"

    return json.dumps(result, ensure_ascii=False, indent=2)