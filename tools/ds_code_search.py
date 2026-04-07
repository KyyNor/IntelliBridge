"""DolphinScheduler 代码检索工具 """

import regex
import json
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime

from utils.mysql_pool import mysql_pool
from utils.logger import logger
from utils.decorators import log_function_info
from fastmcp import FastMCP

ds_search_mcp = FastMCP("IntelliBridge DolphinScheduler Search")


class DataFactoryCodeSearch:
    """DolphinScheduler 代码检索工具类"""

    MAX_LIMIT = 1000
    DEFAULT_PAGE_SIZE = 50
    REGEX_TIMEOUT = 5  # 秒
    CACHE_REFRESH_INTERVAL = 8 * 60 * 60  # 8小时 = 28800秒

    def __init__(self):
        # 主缓存：以 code_path 为唯一键，类似文件系统路径
        self._cache: Dict[str, Dict] = {}

        # 辅助索引：按输入/输出表索引（加速表级别的查询）
        self._index_by_from_table: Dict[str, List[str]] = {}
        self._index_by_to_table: Dict[str, List[str]] = {}

        # 缓存状态
        self._cache_loaded = False
        self._last_load_time: Optional[datetime] = None

        # 定时刷新线程
        self._refresh_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

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
                # 从数据库查询全量数据
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
                    WHERE sql_code IS NOT NULL AND sql_code != ''
                """

                with mysql_pool.get_connection("mysql_121_data_factory") as conn:
                    cursor = conn.cursor()
                    cursor.execute(sql, [])
                    results = cursor.fetchall()
                    cursor.close()

                # 清空旧缓存
                self._cache.clear()
                self._index_by_from_table.clear()
                self._index_by_to_table.clear()

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

                # 第二步：为每个分组构建缓存对象，合并输入输出表
                for group_key, rows in grouped_tasks.items():
                    project_name, process_name, task_name, process_status, task_status, sql_code, lineage_type = group_key

                    # 构造 code_path
                    code_path = f"/ds/{project_name}/{process_name}/{task_name}"

                    # 合并所有行的输入表（去重）
                    from_tables_set = set()
                    to_tables_set = set()

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

                    from_tables = sorted(from_tables_set)  # 转列表并排序，保证一致性
                    to_tables = sorted(to_tables_set)

                    # 按行拆分的代码（便于正则匹配）
                    sql_lines = sql_code.split('\n')

                    # 计算任务状态
                    task_status_value = self.determine_task_status(process_status, task_status)

                    # 构建缓存对象
                    task_obj = {
                        "project_name": project_name,
                        "process_name": process_name,
                        "task_name": task_name,
                        "process_status": process_status,
                        "task_status": task_status,
                        "lineage_type": lineage_type,
                        "from_database_table": from_tables,
                        "to_database_table": to_tables,
                        "sql_code": sql_code,
                        "sql_lines": sql_lines,
                        "任务状态": task_status_value
                    }

                    # 存入主缓存（key就是完整的code_path路径字符串）
                    self._cache[code_path] = task_obj

                    # 建立表级别索引
                    self._build_table_index(code_path, task_obj)

                self._cache_loaded = True
                self._last_load_time = datetime.now()
                logger.info(f"缓存加载完成，共 {len(self._cache)} 条任务")

                # 启动定时刷新
                self._schedule_refresh()

                return True

            except Exception as e:
                logger.error(f"缓存加载失败: {e}")
                return False

    def _build_table_index(self, code_path: str, task_obj: Dict):
        """构建表级别索引"""
        # 按输入表索引
        for table in task_obj.get("from_database_table", []):
            if table not in self._index_by_from_table:
                self._index_by_from_table[table] = []
            self._index_by_from_table[table].append(code_path)

        # 按输出表索引
        for table in task_obj.get("to_database_table", []):
            if table not in self._index_by_to_table:
                self._index_by_to_table[table] = []
            self._index_by_to_table[table].append(code_path)

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
        combined_pattern = f"({escaped_filter})|(^/ds/{escaped_filter})"

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
        # 统计项目数量（通过路径第二级目录）
        projects = set()
        for path in self._cache.keys():
            parts = path.split('/')
            if len(parts) >= 3:
                projects.add(parts[2])

        return {
            "loaded": self._cache_loaded,
            "task_count": len(self._cache),
            "last_load_time": self._last_load_time.isoformat() if self._last_load_time else None,
            "project_count": len(projects),
            "from_table_count": len(self._index_by_from_table),
            "to_table_count": len(self._index_by_to_table)
        }

    # ========== Task 4: 基于缓存实现 datafactory_sql_search ==========

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
        搜索 SQL 代码（基于内存缓存）

        Args:
            pattern: 正则表达式
            code_path: 路径过滤（作为字符串前缀/包含匹配）
            before: 匹配行前的行数
            after: 匹配行后的行数
            code_status: 状态过滤
            page: 页码
            page_size: 每页条数

        Returns:
            包含 matches 和 pagination 的字典
        """
        # 确保缓存已加载
        self.ensure_cache()
        logger.info(f"开始搜索: pattern={pattern}, code_path={code_path}, code_status={code_status}")

        # 1. 通过路径过滤获取候选任务列表（直接在所有keys上做字符串匹配）
        candidate_paths = [
            p for p in self._cache.keys()
            if self._match_path(p, code_path)
        ]

        # 2. 应用状态过滤
        if code_status == "已上线":
            candidate_paths = [
                cp for cp in candidate_paths
                if self._cache.get(cp, {}).get("任务状态") == "已上线"
            ]
        elif code_status == "未上线":
            candidate_paths = [
                cp for cp in candidate_paths
                if self._cache.get(cp, {}).get("任务状态") == "未上线"
            ]

        # 3. 编译正则表达式（启用忽略大小写匹配）
        try:
            compiled_regex = regex.compile(pattern, flags=regex.IGNORECASE)
        except regex.error as e:
            logger.error(f"正则表达式编译失败: {e}")
            return {"matches": [], "pagination": {}, "error": f"正则表达式错误: {e}"}

        # 4. 在内存中进行正则匹配
        task_matches = {}

        for code_path_key in candidate_paths:
            task_obj = self._cache.get(code_path_key)
            if not task_obj:
                continue

            sql_lines = task_obj.get("sql_lines", [])
            matched_lines = []

            for line_num, line in enumerate(sql_lines, start=1):
                try:
                    match = compiled_regex.search(line)
                    if match:
                        # 提取上下文
                        start_idx = max(0, line_num - 1 - before)
                        end_idx = min(len(sql_lines), line_num - 1 + after + 1)
                        context_lines = sql_lines[start_idx:end_idx]

                        matched_lines.append({
                            "行号": line_num,
                            "代码片段": '\n'.join(context_lines)
                        })
                except regex.Timeout:
                    logger.warning(f"正则匹配超时: {code_path_key}, 行 {line_num}")
                    break

            if matched_lines:
                task_matches[code_path_key] = {
                    "code_path": code_path_key,
                    "任务状态": task_obj["任务状态"],
                    "lineage_type": task_obj["lineage_type"],
                    "matched_lines": matched_lines,
                    "from_tables": task_obj.get("from_database_table", []),
                    "to_tables": task_obj.get("to_database_table", [])
                }

        # 5. 转换为 matches 列表
        matches = []
        for cp, info in task_matches.items():
            match_count = len(info["matched_lines"])

            # 所有匹配行的上下文（每个匹配行都有自己前后文）
            all_contexts = [
                {
                    "行号": m["行号"],
                    "代码片段": m["代码片段"]
                }
                for m in info["matched_lines"]
            ]

            matches.append({
                "code_path": cp,
                "匹配行数": match_count,
                "行号": [m["行号"] for m in info["matched_lines"]],
                "所有匹配行上下文": all_contexts,  # 新增：全部匹配行的上下文
                "代码片段": all_contexts[0]["代码片段"] if all_contexts else "",  # 保持兼容，只留第一个
                "任务状态": info["任务状态"],
                "lineage_type": info["lineage_type"],
                "from_database_table": info["from_tables"],
                "to_database_table": info["to_tables"]
            })

        logger.info(f"正则匹配完成，共找到 {len(matches)} 个任务包含匹配")

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

    # ========== Task 5: 基于缓存实现 datafactory_task_info ==========

    def query_task_info(
        self,
        code_path: Optional[str] = None,
        from_table: Optional[str] = None,
        to_table: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        查询任务详情（基于内存缓存）

        Args:
            code_path: 路径筛选（字符串前缀/包含匹配）
            from_table: 输入表名称筛选，支持模糊匹配
            to_table: 输出表名称筛选，支持模糊匹配

        Returns:
            任务详情列表
        """
        # 确保缓存已加载
        self.ensure_cache()
        logger.info(f"查询任务详情: code_path={code_path}, from_table={from_table}, to_table={to_table}")

        # 参数校验：至少需要一个筛选条件
        if not code_path and not from_table and not to_table:
            return [{"error": "请至少填写一个筛选条件"}]

        # 获取候选任务列表
        candidate_paths = set()

        # 1. 按路径过滤（直接字符串匹配）
        if code_path:
            for path in self._cache.keys():
                if self._match_path(path, code_path):
                    candidate_paths.add(path)

        # 2. 按输入表过滤（regex 直接匹配，忽略大小写）
        if from_table:
            temp = set()
            escaped = regex.escape(from_table.strip())
            pattern = regex.compile(escaped, flags=regex.IGNORECASE)
            for table, paths in self._index_by_from_table.items():
                # 直接在整个表名上用 regex 匹配
                if pattern.search(table):
                    temp.update(paths)
            candidate_paths = candidate_paths.intersection(temp) if candidate_paths else temp

        # 3. 按输出表过滤（regex 直接匹配，忽略大小写）
        if to_table:
            temp = set()
            escaped = regex.escape(to_table.strip())
            pattern = regex.compile(escaped, flags=regex.IGNORECASE)
            for table, paths in self._index_by_to_table.items():
                if pattern.search(table):
                    temp.update(paths)
            candidate_paths = candidate_paths.intersection(temp) if candidate_paths else temp

        # 重要修正：如果传入了筛选条件但没匹配到，应该返回空，不要返回全量！
        # 这样用户才能知道自己的查询条件没有命中
        if not candidate_paths:
            logger.info("没有匹配到符合条件的任务")
            return []

        # 构建返回结果
        tasks = []
        for code_path_key in candidate_paths:
            task_obj = self._cache.get(code_path_key)
            if not task_obj:
                continue

            # 截取前100行
            sql_code = task_obj.get("sql_code", "") or ""
            code_lines = sql_code.split('\n')
            code_preview = '\n'.join(code_lines[:100])
            total_lines = len(code_lines)

            tasks.append({
                "code_path": code_path_key,
                "任务类型": task_obj.get("lineage_type", "SQL"),
                "代码": code_preview,
                "代码行数": total_lines,
                "任务状态": task_obj.get("任务状态", "未知"),
                "输入表清单": task_obj.get("from_database_table", []),
                "输出表清单": task_obj.get("to_database_table", [])
            })

        logger.info(f"查询完成，共返回 {len(tasks)} 条任务")
        return tasks

# 默认实例
ds_code_search = DataFactoryCodeSearch()


# ==================== MCP 工具 ====================

@ds_search_mcp.tool()
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
    数据工厂代码检索工具

    在任务代码中进行正则检索，快速找到包含特定 SQL 的代码段。

    典型应用场景：
    - 想看看某个项目中哪些任务使用了某张表（如 FROM dim_xxx）
    - 想找包含特定 SQL 模式的任务（如 JOIN、窗口函数 INSERT INTO 等）
    - 想查看某段代码的上下文（前后几行的代码）

    Args:
        pattern: 正则表达式，用于匹配代码中的特定模式，如 "FROM\\s+\\w+"、"JOIN\\s+\\w+"、"INSERT\\s+INTO" 等
        code_path: 路径筛选，限定搜索范围，支持：
                   - 前缀匹配：如 "/ds/公共每日跑批/" 搜索该目录下所有任务
                   - 关键词匹配：如 "对公有效户" 会匹配路径中任意层级包含该关键词的任务
        before: 整数，匹配行往前显示的行数，用于查看上下文（可选，默认0）
        after: 整数，匹配行往后显示的行数，用于查看上下文（可选，默认0）
        code_status: 状态过滤，可选 "已上线"(默认)、"未上线"，只会返回相应状态的任务
        page: 页码，从1开始（可选，默认1）
        page_size: 每页返回的任務數，默认50条，最大1000（可选）

    Returns:
        JSON 格式的搜索结果，包含：
        - matches: 匹配的任務列表，每条包含 code_path、行号、代码片段、上下游表信息等
        - pagination: 分页信息（page、page_size、total、total_pages）
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


@ds_search_mcp.tool()
@log_function_info
def datafactory_task_info(
    code_path: Optional[str] = None,
    from_table: Optional[str] = None,
    to_table: Optional[str] = None
) -> str:
    """
    数据工厂DolphinScheduler 任务详情查询工具

    查询任务的详细信息，包括任务代码、上下游表清单、任务状态等。
    常用于了解某个任务的用途、数据来源和数据去向。

    典型应用场景：
    - 想知道某个任务是做什么的、代码长什么样 → 用 code_path 查询
    - 想知道哪些任务用了某张表作为输入 → 用 from_table 查询
    - 想知道哪些任务产出了某张表（下游是谁）→ 用 to_table 查询
    - 想追踪某一类数据的完整链路 → 结合多个参数联合查询

    Args:
        code_path: 路径筛选，可精确也可模糊，如：
                   - 精确路径："/ds/公共每日跑批/对公有效户基础户机构汇总/对公户基数调整"
                   - 模糊匹配：如 "对公有效户" 会匹配所有路径中包含该关键词的任务（项目名、工作流名、任务名均可）
        from_table: 输入表名称筛选，只返回使用了该表作为输入的任务，支持模糊匹配（如 "dim_" 会匹配所有包含 dim_ 的表）
        to_table: 输出表名称筛选，只返回将该表作为输出的任务，支持模糊匹配（如 "dws_" 会匹配所有 dws 开头的表）

    Returns:
        JSON 格式的任务详情列表，每个元素包含：
        - code_path: 任务完整路径
        - 任务类型：SQL 或 SHELL_SQL
        - 代码：任务的前100行代码
        - 代码行数：总行数
        - 任务状态：已上线/未上线
        - 输入表清单：该任务依赖的输入表列表
        - 输出表清单：该任务产出的输出表列表

    注意：code_path、from_table、to_table 三个参数至少需要填写一个。
    """
    result = ds_code_search.query_task_info(
        code_path=code_path,
        from_table=from_table,
        to_table=to_table
    )

    if result and isinstance(result, list) and "error" in result[0]:
        return f"错误: {result[0]['error']}"

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