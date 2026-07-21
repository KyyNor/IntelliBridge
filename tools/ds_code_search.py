"""DolphinScheduler 代码检索工具 """

import regex
import json
import threading
import time
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

from utils.mysql_pool import mysql_pool
from utils.logger import logger
from utils.decorators import log_function_info
from utils.cache_snapshot import AtomicSnapshot
from utils.sql_overview import summarize_sql
from fastmcp import FastMCP

ds_search_mcp = FastMCP("IntelliBridge DolphinScheduler Search")


class DataFactoryCodeSearch:
    """DolphinScheduler 代码检索工具类"""

    # 需要展示数据源信息的 lineage_type 白名单
    DATASOURCE_TYPES = frozenset({"SQL", "SHELL_SQL", "SHELL_SQOOP", "SHELL_TRINO_SYNC"})

    TASK_INFO_DEFAULT_PAGE_SIZE = 20
    TASK_INFO_MAX_PAGE_SIZE = 50
    REGEX_TIMEOUT = 5  # 秒
    REGEX_TOTAL_TIMEOUT = 30  # 单次 SQL 搜索总预算（秒）
    CACHE_REFRESH_INTERVAL = 8 * 60 * 60  # 8小时 = 28800秒

    def __init__(self):
        # 主缓存：以 code_path 为唯一键，类似文件系统路径
        # (主缓存, 输入表索引, 输出表索引) 作为一个整体发布。
        self._snapshot = AtomicSnapshot(({}, {}, {}))

        # 缓存状态
        self._cache_loaded = False
        self._last_load_time: Optional[datetime] = None

        # 定时刷新线程
        self._refresh_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

        # SQL overview 按任务路径和 SQL 内容缓存；缓存刷新后整体清理。
        self._overview_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._overview_lock = threading.Lock()

    # ========== 路径匹配辅助方法（简化版：直接当字符串匹配）==========
    # code_path 就是完整的路径字符串，如 /ds/ODS/日报工作流/用户表同步
    # 只需要简单的字符串前缀匹配或包含匹配即可

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

    # ========== 缓存管理方法 ==========

    def load_cache(self, force: bool = False) -> bool:
        """
        加载数据到内存缓存

        Args:
            force: 是否强制重新加载

        Returns:
            是否加载成功
        """
        with self._lock:
            if self._cache_loaded and not force:
                logger.info("缓存已加载，跳过")
                return True

            logger.info("开始加载血缘数据到缓存...")
            try:
                # 从数据库查询全量数据（已扩展新字段）
                sql = """
                    SELECT
                        project_name,
                        process_name,
                        task_name,
                        process_status,
                        task_status,
                        from_source,
                        from_host,
                        to_source,
                        to_host,
                        from_database_table,
                        to_database_table,
                        sql_code,
                        lineage_type
                    FROM metadata_ds_table_lineage
                """

                with mysql_pool.get_connection("mysql_121_data_factory") as conn:
                    cursor = conn.cursor()
                    cursor.execute(sql, [])
                    results = cursor.fetchall()
                    cursor.close()

                # 在新字典中完整构建，完成后再一次性发布。
                new_cache: Dict[str, Dict] = {}
                new_index_by_from_table: Dict[str, List[str]] = {}
                new_index_by_to_table: Dict[str, List[str]] = {}

                # 第一步：按关键字段分组，把相同任务的多条记录合并
                grouped_tasks = {}  # {(project_name, process_name, task_name, process_status, task_status, sql_code, lineage_type): [rows...]}

                for row in results:
                    # 生成group key
                    group_key = (
                        row['project_name'],
                        row['process_name'],
                        row['task_name'],
                        str(row.get('process_status')),
                        str(row.get('task_status')),
                        row.get('sql_code', '') or '',
                        row.get('lineage_type', 'SQL')
                    )

                    if group_key not in grouped_tasks:
                        grouped_tasks[group_key] = []
                    grouped_tasks[group_key].append(row)

                # 第二步：为每个分组构建缓存对象，合并输入输出表，并采集数据源信息
                for group_key, rows in grouped_tasks.items():
                    project_name, process_name, task_name, process_status, task_status, sql_code, lineage_type = group_key

                    # 构造 code_path
                    code_path = f"/ds/{project_name}/{process_name}/{task_name}"

                    # 合并所有行的输入表（去重）
                    from_tables_set = set()
                    to_tables_set = set()

                    # 数据源字段：从同组多条记录中各自取值，取第一个非空（同一任务一般一致）
                    from_source_val = None
                    from_host_val = None
                    to_source_val = None
                    to_host_val = None

                    for row in rows:
                        if row.get('from_database_table'):
                            for t in row['from_database_table'].split(','):
                                t = t.strip()
                                if t:
                                    from_tables_set.add(t)
                        if row.get('to_database_table'):
                            for t in row['to_database_table'].split(','):
                                t = t.strip()
                                if t:
                                    to_tables_set.add(t)

                        # 取第一条非空的来源信息
                        if from_source_val is None and row.get('from_source'):
                            from_source_val = row['from_source']
                        if from_host_val is None and row.get('from_host'):
                            from_host_val = row['from_host']
                        if to_source_val is None and row.get('to_source'):
                            to_source_val = row['to_source']
                        if to_host_val is None and row.get('to_host'):
                            to_host_val = row['to_host']

                    from_tables = sorted(from_tables_set)  # 转列表并排序，保证一致性
                    to_tables = sorted(to_tables_set)

                    # 按行拆分的代码（便于正则匹配）
                    sql_lines = (sql_code or '').split('\n')

                    # 计算任务状态
                    task_status_value = self.determine_task_status(process_status, task_status)

                    # 构建缓存对象（已扩展数据源字段）
                    task_obj = {
                        "project_name": project_name,
                        "process_name": process_name,
                        "task_name": task_name,
                        "process_status": process_status,
                        "task_status": task_status,
                        "lineage_type": lineage_type,
                        "from_database_table": from_tables,
                        "to_database_table": to_tables,
                        "sql_code": sql_code or '',
                        "sql_lines": sql_lines,
                        "任务状态": task_status_value,
                        "from_source": from_source_val,
                        "from_host": from_host_val,
                        "to_source": to_source_val,
                        "to_host": to_host_val,
                    }

                    # 存入主缓存（key就是完整的code_path路径字符串）
                    new_cache[code_path] = task_obj

                    # 建立表级别索引
                    self._build_table_index(
                        code_path,
                        task_obj,
                        new_index_by_from_table,
                        new_index_by_to_table,
                    )

                self._snapshot.replace((
                    new_cache,
                    new_index_by_from_table,
                    new_index_by_to_table,
                ))
                with self._overview_lock:
                    self._overview_cache.clear()
                self._cache_loaded = True
                self._last_load_time = datetime.now()
                logger.info(f"缓存加载完成，共 {len(new_cache)} 条任务")

                # 启动定时刷新
                self._schedule_refresh()

                return True

            except Exception as e:
                logger.error(f"缓存加载失败: {e}")
                return False

    def _build_table_index(
        self,
        code_path: str,
        task_obj: Dict,
        index_by_from_table: Dict[str, List[str]],
        index_by_to_table: Dict[str, List[str]],
    ):
        """构建表级别索引"""
        # 按输入表索引
        for table in task_obj.get("from_database_table", []):
            if table not in index_by_from_table:
                index_by_from_table[table] = []
            index_by_from_table[table].append(code_path)

        # 按输出表索引
        for table in task_obj.get("to_database_table", []):
            if table not in index_by_to_table:
                index_by_to_table[table] = []
            index_by_to_table[table].append(code_path)

    def _match_path(self, full_path: str, filter_path: str) -> bool:
        """
        判断路径是否匹配过滤器

        支持三种匹配方式：
        1. 前缀匹配：filter_path="/ds/ODS/" 匹配所有以此开头的路径
        2. 包含匹配：filter_path="ODS" 匹配路径中任意一段包含此字符串
        3. 工作流级匹配：如果 filter_path 匹配到工作流名，则返回该工作流下所有任务

        例如：filter_path="/ds/对公有效户/"
        - 会匹配到工作流 "2025对公劳动竞赛大屏-邓安琦"，然后返回其下所有任务
        - 也会匹配到任务名包含"对公有效户"的任务

        Args:
            full_path: 完整的任务路径，如 /ds/ODS/日报工作流/用户表同步
            filter_path: 过滤路径，如 /ds/ODS/ 或 ODS

        Returns:
            是否匹配
        """
        # 清理过滤路径
        filter_path = filter_path.strip().rstrip('/')

        # 空过滤器或只有 /ds 表示匹配全部
        if not filter_path or filter_path == '/ds':
            return True

        # 使用 regex 进行模糊匹配（忽略大小写），而不是拆分路径逐段匹配
        # 将 filter 转为正则表达式（原样匹配，忽略大小写）
        escaped_filter = regex.escape(filter_path)

        # 直接在整个路径上进行 regex 搜索（忽略大小写）
        # 匹配方式：
        # 1. filter 是路径的前缀
        # 2. filter 出现在路径的任意位置（模糊匹配）
        # 直接用 regex 做模糊匹配（忽略大小写）
        # 匹配方式：filter 出现在路径的任意位置
        if regex.search(escaped_filter, full_path, flags=regex.IGNORECASE):
            return True

        return False

    def _schedule_refresh(self):
        """调度定时刷新"""
        if self._refresh_timer:
            self._refresh_timer.cancel()

        self._refresh_timer = threading.Timer(
            self.CACHE_REFRESH_INTERVAL,
            self.load_cache,
            kwargs={"force": True}
        )
        self._refresh_timer.daemon = True
        self._refresh_timer.start()
        logger.info(f"已调度缓存刷新，间隔 {self.CACHE_REFRESH_INTERVAL} 秒")

    def ensure_cache(self):
        """确保缓存已加载（懒加载）"""
        if not self._cache_loaded:
            self.load_cache()

    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        cache, index_by_from_table, index_by_to_table = self._snapshot.get()
        # 统计项目数量（通过路径第二级目录）
        projects = set()
        for path in cache.keys():
            parts = path.split('/')
            if len(parts) >= 3:
                projects.add(parts[2])

        return {
            "loaded": self._cache_loaded,
            "task_count": len(cache),
            "last_load_time": self._last_load_time.isoformat() if self._last_load_time else None,
            "project_count": len(projects),
            "from_table_count": len(index_by_from_table),
            "to_table_count": len(index_by_to_table)
        }

    # ========== Task 4: 基于缓存实现 datafactory_sql_search ==========

    # 最大上下文行数限制，防止返回过多内容超出上下文范围
    MAX_TOTAL_CONTEXT_LINES = 100

    # 起止行模式最多返回的行数。该模式用于读取代码，不走正则匹配。
    MAX_RANGE_LINES = 100

    # 单个任务最多返回的匹配行数。超过后只截断匹配结果，不改为 SQL 概览。
    MAX_MATCHED_LINES = 100

    def _normalize_exact_code_path(self, code_path: Optional[str]) -> str:
        """规范化精确任务路径；允许忽略末尾斜杠。"""
        if not code_path:
            return ""
        return code_path.strip().rstrip("/")

    def _get_exact_task(
        self,
        cache: Dict[str, Dict],
        code_path: Optional[str],
    ) -> tuple[str, Optional[Dict]]:
        """按完整 code_path 获取单个任务，不做模糊匹配。"""
        normalized_path = self._normalize_exact_code_path(code_path)
        if not normalized_path or normalized_path == "/ds":
            return normalized_path, None
        return normalized_path, cache.get(normalized_path)

    def _build_task_overview(self, code_path: str, task_obj: Dict) -> Dict[str, Any]:
        """构建单个任务的 SQL 结构概览，不返回 SQL 原文。"""
        sql_code = task_obj.get("sql_code", "") or ""
        cache_key = (code_path, sql_code)
        with self._overview_lock:
            cached = self._overview_cache.get(cache_key)
            if cached is not None:
                return cached

            summary = summarize_sql(sql_code)
            overview = {
                "code_path": code_path,
                "任务状态": task_obj.get("任务状态", "未知"),
                "lineage_type": task_obj.get("lineage_type", ""),
                "parse_ok": summary["parse_ok"],
                "statement_count": summary["statement_count"],
                "statements": summary["statements"],
                "from_database_table": task_obj.get("from_database_table", []),
                "to_database_table": task_obj.get("to_database_table", []),
            }
            self._overview_cache[cache_key] = overview
            return overview

    def _build_empty_pattern_fallback(
        self,
        code_path: str,
        task_obj: Dict,
    ) -> Dict[str, Any]:
        """返回空 pattern 的结构化结果，避免把整段 SQL 当作空正则返回。"""
        return {
            "fallback": True,
            "fallback_reason": "empty_pattern",
            "message": (
                "因为 pattern 为空，无法确定需要查看的代码范围，所以只返回该任务的 SQL 结构概览。"
                "如果需要查看更多代码，请提供更精确的 pattern，或指定 start_line 和 end_line。"
            ),
            "code_path": code_path,
            "overview": self._build_task_overview(code_path, task_obj),
            "pagination": {
                "page": 1,
                "page_size": 1,
                "total": 1,
                "total_pages": 1,
            },
        }

    def _read_sql_range(
        self,
        cache: Dict[str, Dict],
        code_path: Optional[str],
        start_line: Optional[int],
        end_line: Optional[int],
        code_status: str,
    ) -> Dict[str, Any]:
        """读取精确任务的连续代码行，行号为 1-based 且包含首尾。"""
        normalized_path, task_obj = self._get_exact_task(cache, code_path)
        if task_obj is None:
            return {
                "matches": [],
                "pagination": {},
                "error": "code_path 必须是已存在的完整任务路径，例如 /ds/项目/工作流/任务",
            }

        if code_status in ("已上线", "未上线") and task_obj.get("任务状态") != code_status:
            return {
                "matches": [],
                "pagination": {"page": 1, "page_size": 1, "total": 0, "total_pages": 0},
            }

        if start_line is None or end_line is None:
            return {
                "matches": [],
                "pagination": {},
                "error": "start_line 和 end_line 必须同时填写",
            }

        if (
            isinstance(start_line, bool)
            or isinstance(end_line, bool)
            or not isinstance(start_line, int)
            or not isinstance(end_line, int)
        ):
            return {
                "matches": [],
                "pagination": {},
                "error": "start_line 和 end_line 必须是正整数",
            }

        if start_line < 1 or end_line < 1 or start_line > end_line:
            return {
                "matches": [],
                "pagination": {},
                "error": "start_line 和 end_line 必须是正整数，且 start_line <= end_line",
            }

        requested_lines = end_line - start_line + 1
        if requested_lines > self.MAX_RANGE_LINES:
            return {
                "matches": [],
                "pagination": {},
                "error": f"单次最多读取 {self.MAX_RANGE_LINES} 行代码",
            }

        sql_lines = task_obj.get("sql_lines", [])
        total_lines = len(sql_lines)
        if start_line > total_lines:
            return {
                "matches": [],
                "pagination": {"page": 1, "page_size": 1, "total": 0, "total_pages": 0},
                "message": f"请求起始行 {start_line} 超出代码总行数 {total_lines}",
            }

        actual_end_line = min(end_line, total_lines)
        context_lines = sql_lines[start_line - 1:actual_end_line]
        match = {
            "code_path": normalized_path,
            "起始行": start_line,
            "结束行": actual_end_line,
            "行号": list(range(start_line, actual_end_line + 1)),
            "代码片段": "\n".join(context_lines),
            "任务状态": task_obj.get("任务状态", "未知"),
            "lineage_type": task_obj.get("lineage_type", ""),
            "from_database_table": task_obj.get("from_database_table", []),
            "to_database_table": task_obj.get("to_database_table", []),
        }
        return {
            "mode": "range",
            "matches": [match],
            "pagination": {"page": 1, "page_size": 1, "total": 1, "total_pages": 1},
        }

    def search_sql_codes(
        self,
        pattern: str = "",
        code_path: Optional[str] = None,
        before: int = 0,
        after: int = 0,
        code_status: str = "已上线",
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        搜索或读取单个精确任务的 SQL 代码。

        pattern 模式按行执行大小写不敏感的 regex 搜索；
        start_line/end_line 模式读取指定的连续代码行，不执行 regex。
        """
        before = max(before, 0)
        after = max(after, 0)
        context_total = before + after
        if context_total > self.MAX_TOTAL_CONTEXT_LINES:
            scale = self.MAX_TOTAL_CONTEXT_LINES / context_total
            before = int(before * scale)
            after = self.MAX_TOTAL_CONTEXT_LINES - before

        self.ensure_cache()
        cache, _, _ = self._snapshot.get()
        logger.info(
            f"开始搜索: pattern={pattern}, code_path={code_path}, "
            f"code_status={code_status}, start_line={start_line}, end_line={end_line}"
        )

        # 起止行是直接读取代码的模式，不执行正则，也不触发空 pattern 回退。
        if start_line is not None or end_line is not None:
            if pattern and pattern.strip():
                return {
                    "matches": [],
                    "pagination": {},
                    "error": "start_line/end_line 行范围模式不能同时指定非空 pattern",
                }
            return self._read_sql_range(
                cache=cache,
                code_path=code_path,
                start_line=start_line,
                end_line=end_line,
                code_status=code_status,
            )

        normalized_path, task_obj = self._get_exact_task(cache, code_path)
        if task_obj is None:
            return {
                "matches": [],
                "pagination": {},
                "error": "code_path 必须是已存在的完整任务路径，例如 /ds/项目/工作流/任务",
            }

        if code_status in ("已上线", "未上线") and task_obj.get("任务状态") != code_status:
            return {
                "matches": [],
                "pagination": {"page": 1, "page_size": 1, "total": 0, "total_pages": 0},
            }

        # 空 pattern 直接返回单个任务的概览，不先执行空正则扫描整段 SQL。
        if not pattern or not pattern.strip():
            return self._build_empty_pattern_fallback(
                code_path=normalized_path,
                task_obj=task_obj,
            )

        search_deadline = time.monotonic() + self.REGEX_TOTAL_TIMEOUT
        try:
            compiled_regex = regex.compile(pattern, flags=regex.IGNORECASE)
        except regex.error as e:
            logger.error(f"正则表达式编译失败: {e}")
            return {
                "matches": [],
                "pagination": {},
                "error": f"正则表达式错误: {e}",
            }

        sql_lines = task_obj.get("sql_lines", [])
        matched_lines = []
        matched_count = 0
        timeout_reason = None
        truncated = False

        for line_num, line in enumerate(sql_lines, start=1):
            remaining = search_deadline - time.monotonic()
            if remaining <= 0:
                timeout_reason = "total"
                break
            try:
                match = compiled_regex.search(
                    line,
                    timeout=min(self.REGEX_TIMEOUT, remaining),
                )
                if not match:
                    continue

                matched_count += 1
                if len(matched_lines) >= self.MAX_MATCHED_LINES:
                    truncated = True
                    continue

                start_idx = max(0, line_num - 1 - before)
                end_idx = min(len(sql_lines), line_num - 1 + after + 1)
                context_lines = sql_lines[start_idx:end_idx]
                matched_lines.append({
                    "行号": line_num,
                    "代码片段": "\n".join(context_lines),
                })
            except TimeoutError:
                logger.warning(f"正则匹配超时: {normalized_path}, 行 {line_num}")
                timeout_reason = "total" if remaining <= self.REGEX_TIMEOUT else "line"
                break

        if not matched_lines:
            result = {
                "matches": [],
                "pagination": {
                    "page": 1,
                    "page_size": 1,
                    "total": 0,
                    "total_pages": 0,
                },
            }
            if timeout_reason:
                message = (
                    f"正则搜索超过 {self.REGEX_TOTAL_TIMEOUT} 秒总预算，已停止继续搜索；"
                    "请缩小代码范围或简化 pattern。"
                    if timeout_reason == "total"
                    else (
                        f"单行正则匹配超过 {self.REGEX_TIMEOUT} 秒，已停止继续搜索；"
                        "请简化 pattern。"
                    )
                )
                result.update({
                    "regex_timeout": True,
                    "message": message,
                })
            return result

        all_contexts = [
            {"行号": item["行号"], "代码片段": item["代码片段"]}
            for item in matched_lines
        ]
        match_result = {
            "code_path": normalized_path,
            "匹配行数": matched_count,
            "返回行数": len(matched_lines),
            "还有更多": truncated,
            "匹配统计完整": timeout_reason is None,
            "行号": [item["行号"] for item in matched_lines],
            "所有匹配行上下文": all_contexts,
            "代码片段": all_contexts[0]["代码片段"],
            "任务状态": task_obj.get("任务状态", "未知"),
            "lineage_type": task_obj.get("lineage_type", ""),
            "from_database_table": task_obj.get("from_database_table", []),
            "to_database_table": task_obj.get("to_database_table", []),
        }
        hints = []
        if truncated:
            match_result["截断"] = True
            hints.append(
                f"该 pattern 共命中至少 {matched_count} 行，只返回前 {self.MAX_MATCHED_LINES} 行；"
                "如需查看更多，请使用更精确的 pattern 或 start_line/end_line。"
            )
        if timeout_reason:
            match_result["正则超时"] = True
            if timeout_reason == "total":
                hints.append(
                    f"正则搜索超过 {self.REGEX_TOTAL_TIMEOUT} 秒总预算，已停止继续搜索；"
                    "请缩小代码范围或简化 pattern。"
                )
            else:
                hints.append(
                    f"单行正则匹配超过 {self.REGEX_TIMEOUT} 秒，已停止继续搜索；"
                    "请简化 pattern。"
                )
        if hints:
            match_result["提示"] = " ".join(hints)

        return {
            "matches": [match_result],
            "pagination": {
                "page": 1,
                "page_size": 1,
                "total": 1,
                "total_pages": 1,
            },
        }

    # ========== Task 5: 基于缓存实现 datafactory_task_info ==========

    @staticmethod
    def _normalize_optional_filter(value: Optional[str]) -> Optional[str]:
        """独立清理可选筛选条件；空白参数不影响其他有效条件。"""
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    def query_task_info(
        self,
        code_path: Optional[str] = None,
        from_table: Optional[str] = None,
        to_table: Optional[str] = None,
        page: int = 1,
        page_size: int = TASK_INFO_DEFAULT_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """
        查询任务详情（基于内存缓存）

        Args:
            code_path: 路径筛选（字符串前缀/包含匹配）
            from_table: 输入表名称筛选，支持模糊匹配
            to_table: 输出表名称筛选，支持模糊匹配
            page: 页码，从 1 开始
            page_size: 每页任务数，默认20，最大50

        Returns:
            包含 tasks、pagination 和 agent 操作提示的字典
        """
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or isinstance(page_size, bool)
            or not isinstance(page_size, int)
        ):
            return {
                "error": "page 和 page_size 必须是整数",
                "tasks": [],
                "pagination": {},
            }

        page = max(1, page)
        page_size = min(max(page_size, 1), self.TASK_INFO_MAX_PAGE_SIZE)

        # 每个筛选条件独立清理：例如 to_table=" " 不会屏蔽有效的 from_table。
        code_path = self._normalize_optional_filter(code_path)
        from_table = self._normalize_optional_filter(from_table)
        to_table = self._normalize_optional_filter(to_table)

        # 确保缓存已加载
        self.ensure_cache()
        cache, index_by_from_table, index_by_to_table = self._snapshot.get()
        logger.info(f"查询任务详情: code_path={code_path}, from_table={from_table}, to_table={to_table}")

        # 参数校验：至少需要一个筛选条件
        if not code_path and not from_table and not to_table:
            return {
                "error": "请至少填写一个筛选条件",
                "tasks": [],
                "pagination": {},
            }

        # 获取候选任务列表。None 表示尚未应用任何筛选条件，
        # 空 set 则表示某个筛选条件已经明确没有命中，二者不能混淆。
        candidate_paths = None

        # 1. 按路径过滤（直接字符串匹配）
        if code_path:
            path_candidates = {
                path for path in cache.keys()
                if self._match_path(path, code_path)
            }
            candidate_paths = path_candidates

        # 2. 按输入表过滤（regex 直接匹配，忽略大小写）
        if from_table:
            temp = set()
            escaped = regex.escape(from_table.strip())
            pattern = regex.compile(escaped, flags=regex.IGNORECASE)
            for table, paths in index_by_from_table.items():
                # 直接在整个表名上用 regex 匹配
                if pattern.search(table):
                    temp.update(paths)
            candidate_paths = temp if candidate_paths is None else candidate_paths.intersection(temp)

        # 3. 按输出表过滤（regex 直接匹配，忽略大小写）
        if to_table:
            temp = set()
            escaped = regex.escape(to_table.strip())
            pattern = regex.compile(escaped, flags=regex.IGNORECASE)
            for table, paths in index_by_to_table.items():
                if pattern.search(table):
                    temp.update(paths)
            candidate_paths = temp if candidate_paths is None else candidate_paths.intersection(temp)

        # 重要修正：如果传入了筛选条件但没匹配到，应该返回空，不要返回全量！
        # 这样用户才能知道自己的查询条件没有命中
        if not candidate_paths:
            logger.info("没有匹配到符合条件的任务")
            return {
                "tasks": [],
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": 0,
                    "returned": 0,
                    "total_pages": 0,
                    "has_previous": False,
                    "has_next": False,
                },
                "hint": "没有匹配到符合条件的任务。",
            }

        # 分页后只为当前页生成 overview，避免一次解析全部候选任务。
        sorted_paths = sorted(candidate_paths)
        total = len(sorted_paths)
        total_pages = (total + page_size - 1) // page_size
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_paths = sorted_paths[start_idx:end_idx]

        # 构建当前页返回结果
        tasks = []
        for code_path_key in page_paths:
            task_obj = cache.get(code_path_key)
            if not task_obj:
                continue

            sql_code = task_obj.get("sql_code", "") or ""
            total_lines = len(sql_code.split('\n'))

            # 仅在白名单内的 lineage_type 才追加数据源相关字段
            lineage_type = task_obj.get("lineage_type", "")

            task_result = {
                "code_path": code_path_key,
                "任务类型": lineage_type,
                "代码行数": total_lines,
                "任务状态": task_obj.get("任务状态", "未知"),
                "输入表清单": task_obj.get("from_database_table", []),
                "输出表清单": task_obj.get("to_database_table", []),
                "sql_overview": self._build_task_overview(code_path_key, task_obj),
            }

            if lineage_type in self.DATASOURCE_TYPES:
                def fmt(val):
                    return val if val else "未知"
                task_result["来源数据类型"] = fmt(task_obj.get("from_source"))
                task_result["来源数据IP"] = fmt(task_obj.get("from_host"))
                task_result["目标数据类型"] = fmt(task_obj.get("to_source"))
                task_result["目标数据IP"] = fmt(task_obj.get("to_host"))

            tasks.append(task_result)

        returned = len(tasks)
        has_previous = page > 1 and total > 0
        has_next = page < total_pages

        if returned == 0:
            hint = (
                f"共 {total} 条任务，但当前是第 {page} 页，未展示到数据；"
                f"请使用 page=1 到 page={total_pages}。"
            )
        elif has_next:
            hint = (
                f"共 {total} 条任务，本页展示 {returned} 条（第 {page}/{total_pages} 页）。"
                f"如需继续查看，请保持筛选条件不变并传 page={page + 1}；"
                f"也可以进一步缩小筛选条件，或调整 page_size（最大{self.TASK_INFO_MAX_PAGE_SIZE}）。"
            )
        else:
            hint = f"共 {total} 条任务，本页展示 {returned} 条（第 {page}/{total_pages} 页），已全部展示。"

        logger.info(
            f"查询完成，共 {total} 条任务，本页返回 {returned} 条，"
            f"page={page}, page_size={page_size}"
        )
        return {
            "tasks": tasks,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "returned": returned,
                "total_pages": total_pages,
                "has_previous": has_previous,
                "has_next": has_next,
            },
            "hint": hint,
        }

# 默认实例
ds_code_search = DataFactoryCodeSearch()


# ==================== MCP 工具 ====================

@ds_search_mcp.tool(name="sql")
@log_function_info
def datafactory_sql_search(
    code_path: str,
    pattern: str = "",
    before: int = 0,
    after: int = 0,
    code_status: str = "已上线",
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    """
    数据工厂代码检索工具

    在单个精确任务中进行正则检索，或读取指定起止行的代码。

    典型应用场景：
    - 想查看某个已知任务中包含特定 SQL 模式的代码（如 JOIN、窗口函数、INSERT INTO）
    - 想查看某段代码的上下文（前后几行的代码）
    - 想直接查看某个任务的第 100-110 行

    Args:
        code_path: 必填，完整且精确的任务路径，如 "/ds/项目/工作流/任务"
        pattern: 正则表达式，用于匹配代码中的特定模式，如 "FROM\\s+\\w+"、"JOIN\\s+\\w+"、"INSERT\\s+INTO" 等。
                普通检索模式下为空会返回该任务的 SQL 结构概览。
        before: 整数，匹配行往前显示的行数，用于查看上下文（可选，默认0，最大50行）
        after: 整数，匹配行往后显示的行数，用于查看上下文（可选，默认0，最大50行，总计不超过100行）
        code_status: 状态过滤，可选 "已上线"(默认)、"未上线"，只会返回相应状态的任务
        start_line: 起始行号，和 end_line 同时填写时读取连续代码，不执行 pattern（1-based）
        end_line: 结束行号，包含该行；单次最多读取100行

    Returns:
        JSON 格式的搜索结果，包含：
        - matches: 匹配或读取到的代码片段
        - fallback: pattern 为空时返回的 SQL 结构概览及原因说明
        - pagination: 单个任务结果统计信息
    """
    result = ds_code_search.search_sql_codes(
        pattern=pattern,
        code_path=code_path,
        before=before,
        after=after,
        code_status=code_status,
        start_line=start_line,
        end_line=end_line,
    )

    if "error" in result:
        return f"错误: {result['error']}"

    return json.dumps(result, ensure_ascii=False, indent=2)


@ds_search_mcp.tool(name="task_info")
@log_function_info
def datafactory_task_info(
    code_path: Optional[str] = None,
    from_table: Optional[str] = None,
    to_table: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> str:
    """
    数据工厂DolphinScheduler 任务详情查询工具

    查询任务的详细信息，包括 SQL 结构概览、上下游表清单、任务状态等。
    常用于了解某个任务的用途、数据来源和数据去向。

    典型应用场景：
    - 想知道某个任务有几条 SQL、有哪些输入输出表、CTE 在哪几行 → 用 code_path 查询
    - 想知道哪些任务用了某张表作为输入 → 用 from_table 查询
    - 想知道哪些任务产出了某张表（下游是谁）→ 用 to_table 查询
    - 想追踪某一类数据的完整链路 → 结合多个参数联合查询

    Args:
        code_path: 路径筛选，可精确也可模糊，如：
                   - 精确路径："/ds/公共每日跑批/对公有效户基础户机构汇总/对公户基数调整"
                   - 模糊匹配：如 "对公有效户" 会匹配所有路径中包含该关键词的任务（项目名、工作流名、任务名均可）
        from_table: 输入表名称筛选，只返回使用了该表作为输入的任务，支持模糊匹配（如 "dim_" 会匹配所有包含 dim_ 的表）
        to_table: 输出表名称筛选，只返回将该表作为输出的任务，支持模糊匹配（如 "dws_" 会匹配所有 dws 开头的表）
        page: 页码，从1开始，默认1
        page_size: 每页任务数，默认20，最多50

    Returns:
        JSON 格式的对象，包含 tasks、pagination 和 hint。
        tasks 中每个元素包含：
        - code_path: 任务完整路径
        - 任务类型：SQL / SHELL_SQL / SHELL_SQOOP / SHELL_TRINO_SYNC
        - sql_overview：SQL 解析得到的语句类型、输入表、输出表、CTE 及行号；不返回 SQL 原文
        - 代码行数：总行数
        - 任务状态：已上线/未上线
        - 输入表清单：该任务依赖的输入表列表
        - 输出表清单：该任务产出的输出表列表
        pagination 会说明 total、returned、page、total_pages、has_next；hint 会告诉 agent 如何继续翻页。

    注意：code_path、from_table、to_table 三个参数至少需要填写一个。
    """
    result = ds_code_search.query_task_info(
        code_path=code_path,
        from_table=from_table,
        to_table=to_table,
        page=page,
        page_size=page_size,
    )

    if isinstance(result, dict) and "error" in result:
        return f"错误: {result['error']}"

    return json.dumps(result, ensure_ascii=False, indent=2)


@log_function_info
def datafactory_refresh_cache(force: bool = False) -> str:
    """
    手动刷新血缘数据缓存

    Args:
        force: 是否强制刷新（即使缓存未过期）

    Returns:
        JSON 格式的刷新结果
    """
    success = ds_code_search.load_cache(force=force)
    stats = ds_code_search.get_cache_stats()

    if success:
        return json.dumps({
            "message": "缓存刷新成功",
            "stats": stats
        }, ensure_ascii=False, indent=2)
    else:
        return json.dumps({
            "message": "缓存刷新失败",
            "stats": stats
        }, ensure_ascii=False, indent=2)


@log_function_info
def datafactory_get_cache_stats() -> str:
    """
    获取缓存统计信息

    返回当前缓存的状态，包括任务数量、最后加载时间等。

    Returns:
        JSON 格式的缓存统计信息
    """
    stats = ds_code_search.get_cache_stats()
    return json.dumps(stats, ensure_ascii=False, indent=2)
