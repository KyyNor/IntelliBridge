# DolphinScheduler 代码检索工具实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现两个 MCP 工具：datafactory_sql_search（类 grep 的代码检索）和 datafactory_task_info（任务详情查询），用于在 DolphinScheduler 任务代码中进行正则检索。采用**内存缓存**架构，启动时一次性加载全量数据，后续查询直接命中内存。

**Architecture:**

- 复用现有 mysql_pool 连接池
- 在 tools/ 目录下新建 ds_code_search.py 模块
- 实现 DataFactoryCodeSearch 类，通过 @mcp.tool() 注册两个工具函数
- 核心：使用内存字典缓存，以 code_path 为唯一键

**Tech Stack:** Python, FastAPI, PyMySQL, regex（带超时控制）, 项目现有工具框架, threading.Timer（定时刷新）

**关键决策：**
- 正则引擎：使用 `regex` 库（支持超时控制）
- **缓存策略**：启动时/懒加载全量数据，后续查询走内存缓存，定时（8小时）/手动刷新
- 日志记录：参考 mysql_query.py 的日志风格，关键步骤 info，异常 error

---

## 准备阶段

### 任务 0: 添加数据库配置

**Files:**
- Modify: `config/config.yaml`

**Step 1: 查看实际的 mysql_137 连接信息**

先检查项目中是否有数据库连接信息文档，或者询问用户获取：
- 主机 IP/域名
- 端口
- 用户名
- 密码

**Step 2: 添加节点配置**

在 `config/config.yaml` 的 `mysql.nodes` 数组中添加新节点：

```yaml
- name: node137
  host: "125.160.1.137"
  port: 3306
  username: "etluser"
  password: "etl_20230605"
  charset: "utf8mb4"
  databases:
    - name: data_factory
      description: "DolphinScheduler 元数据&血缘数据"
```

> 注意：数据库访问标识符将是 `node137_data_factory`

**Step 3: 运行验证**

检查服务能否正常启动，无报错即可。

---

## 主体实现

### Task 1: 创建带缓存的数据工厂查询模块结构

**Files:**
- Create/Modify: `tools/ds_code_search.py`

**Step 1: 编写缓存架构代码框架**

```python
"""DolphinScheduler 代码检索工具"""

import regex
import json
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime

from utils.mysql_pool import mysql_pool
from utils.logger import logger
from utils.decorators import log_function_info


class DataFactoryCodeSearch:
    """DolphinScheduler 代码检索工具类（带内存缓存）"""

    MAX_LIMIT = 1000
    DEFAULT_PAGE_SIZE = 50
    REGEX_TIMEOUT = 5  # 秒
    CACHE_REFRESH_INTERVAL = 8 * 60 * 60  # 8小时 = 28800秒

    def __init__(self):
        # 主缓存：以 code_path 为唯一键
        self._cache: Dict[str, Dict] = {}

        # 辅助索引（加快特定维度查询）
        self._index_by_project: Dict[str, List[str]] = {}
        self._index_by_process: Dict[str, List[str]] = {}
        self._index_by_from_table: Dict[str, List[str]] = {}
        self._index_by_to_table: Dict[str, List[str]] = {}

        # 缓存状态
        self._cache_loaded = False
        self._last_load_time: Optional[datetime] = None

        # 定时刷新线程
        self._refresh_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

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

                with mysql_pool.get_connection("local_node_data_factory") as conn:
                    cursor = conn.cursor()
                    cursor.execute(sql, [])
                    results = cursor.fetchall()

                # 清空旧缓存
                self._cache.clear()
                self._index_by_project.clear()
                self._index_by_process.clear()
                self._index_by_from_table.clear()
                self._index_by_to_table.clear()

                # 构建缓存和索引
                for row in results:
                    code_path = f"/ds/{row['project_name']}/{row['process_name']}/{row['task_name']}"

                    # 解析表清单
                    from_tables = []
                    if row.get('from_database_table'):
                        from_tables = [t.strip() for t in row['from_database_table'].split(',') if t.strip()]

                    to_tables = []
                    if row.get('to_database_table'):
                        to_tables = [t.strip() for t in row['to_database_table'].split(',') if t.strip()]

                    # 按行拆分的代码（便于正则匹配）
                    sql_code = row.get('sql_code', '') or ''
                    sql_lines = sql_code.split('\n')

                    # 计算任务状态
                    task_status = self.determine_task_status(
                        str(row.get('process_status')),
                        str(row.get('task_status'))
                    )

                    # 构建缓存对象
                    task_obj = {
                        "project_name": row['project_name'],
                        "process_name": row['process_name'],
                        "task_name": row['task_name'],
                        "process_status": str(row.get('process_status')),
                        "task_status": str(row.get('task_status')),
                        "lineage_type": row.get('lineage_type', 'SQL'),
                        "from_database_table": from_tables,
                        "to_database_table": to_tables,
                        "sql_code": sql_code,
                        "sql_lines": sql_lines,
                        "任务状态": task_status
                    }

                    # 存入主缓存
                    self._cache[code_path] = task_obj

                    # 建立辅助索引
                    self._build_index(code_path, task_obj)

                self._cache_loaded = True
                self._last_load_time = datetime.now()
                logger.info(f"缓存加载完成，共 {len(self._cache)} 条任务")

                # 启动定时刷新
                self._schedule_refresh()

                return True

            except Exception as e:
                logger.error(f"缓存加载失败: {e}")
                return False

    def _build_index(self, code_path: str, task_obj: Dict):
        """构建辅助索引"""
        project = task_obj["project_name"]
        process = task_obj["process_name"]

        # 按项目索引
        if project not in self._index_by_project:
            self._index_by_project[project] = []
        self._index_by_project[project].append(code_path)

        # 按工作流索引
        if process not in self._index_by_process:
            self._index_by_process[process] = []
        self._index_by_process[process].append(code_path)

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
        return {
            "loaded": self._cache_loaded,
            "task_count": len(self._cache),
            "last_load_time": self._last_load_time.isoformat() if self._last_load_time else None,
            "project_count": len(self._index_by_project),
            "process_count": len(self._index_by_process)
        }


# 默认实例
ds_code_search = DataFactoryCodeSearch()


# ==================== MCP 工具 ====================

@mcp.tool()
@log_function_info
def datafactory_sql_search(...):
    pass


@mcp.tool()
@log_function_info
def datafactory_task_info(...):
    pass


@mcp.tool()
@log_function_info
def datafactory_refresh_cache(...) -> str:
    """手动刷新缓存"""
    pass
```

**Step 2: 运行验证**

执行 `python -c "from tools.ds_code_search import ds_code_search; print('OK')"` 确认模块导入无误。

**Step 3: Commit**

```bash
git add tools/ds_code_search.py
git commit -m "feat: init ds_code_search module with cache architecture"
```

---

### Task 2: 实现 code_path 解析辅助方法

**Files:**
- Modify: `tools/ds_code_search.py`

保持原有实现不变（已在上一版本实现）。

---

### Task 3: 实现任务状态判定方法

**Files:**
- Modify: `tools/ds_code_search.py`

保持原有实现不变（已在上一版本实现）。

---

### Task 4: 基于缓存实现 datafactory_sql_search

**Files:**
- Modify: `tools/ds_code_search.py`

**Step 1: 实现内存查询逻辑**

```python
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
        code_path: 路径过滤
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

    # 1. 解析 code_path 进行预筛选
    parsed = self.parse_code_path(code_path)

    # 2. 获取候选任务列表
    candidate_paths = self._get_candidate_paths(parsed, code_status)

    # 3. 编译正则表达式
    try:
        compiled_regex = regex.compile(pattern, timeout=self.REGEX_TIMEOUT)
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
        sample_line = info["matched_lines"][0] if info["matched_lines"] else {}

        matches.append({
            "code_path": cp,
            "匹配行数": match_count,
            "行号": [m["行号"] for m in info["matched_lines"]],
            "代码片段": sample_line.get("代码片段", ""),
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

def _get_candidate_paths(self, parsed: Dict, code_status: str) -> List[str]:
    """获取候选任务路径列表"""
    candidates = []

    # 如果有明确的路径过滤，使用索引快速定位
    if parsed["project"] and parsed["project"] in self._index_by_project:
        candidates = self._index_by_project[parsed["project"]]
    elif parsed["process"] and parsed["process"] in self._index_by_process:
        candidates = self._index_by_process[parsed["process"]]
    else:
        # 否则遍历全量缓存
        candidates = list(self._cache.keys())

    # 应用状态过滤
    if code_status == "已上线":
        candidates = [
            cp for cp in candidates
            if self._cache.get(cp, {}).get("任务状态") == "已上线"
        ]
    elif code_status == "未上线":
        candidates = [
            cp for cp in candidates
            if self._cache.get(cp, {}).get("任务状态") == "未上线"
        ]

    # 进一步过滤精确匹配
    if parsed["task"]:
        # 精确到任务名
        filtered = []
        for cp in candidates:
            task_obj = self._cache.get(cp, {})
            if parsed["task"].lower() in task_obj.get("task_name", "").lower():
                filtered.append(cp)
        candidates = filtered

    return candidates
```

**Step 2: 注册 MCP 工具**

```python
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
    DolphinScheduler 代码检索工具（类 grep）- 基于内存缓存

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
```

**Step 3: Commit**

```bash
git add tools/ds_code_search.py
git commit -m "feat: implement datafactory_sql_search with memory cache"
```

---

### Task 5: 基于缓存实现 datafactory_task_info

**Files:**
- Modify: `tools/ds_code_search.py`

**Step 1: 实现内存查询逻辑**

```python
def query_task_info(
    self,
    code_path: Optional[str] = None,
    from_table: Optional[str] = None,
    to_table: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    查询任务详情（基于内存缓存）

    Args:
        code_path: 路径筛选，支持精确或模糊匹配
        from_table: 输入表名称筛选，支持模糊匹配
        to_table: 输出表名称筛选，支持模糊匹配

    Returns:
        任务详情列表
    """
    # 确保缓存已加载
    self.ensure_cache()

    # 参数校验：至少需要一个筛选条件
    if not code_path and not from_table and not to_table:
        return [{"error": "请至少填写一个筛选条件"}]

    # 获取候选任务列表
    candidate_paths = set()

    if code_path:
        parsed = self.parse_code_path(code_path)

        if parsed["project"]:
            # 使用项目索引
            for proj, paths in self._index_by_project.items():
                if parsed["project"].lower() in proj.lower():
                    candidate_paths.update(paths)

        if parsed["process"]:
            # 使用工作流索引
            temp = set()
            for proc, paths in self._index_by_process.items():
                if parsed["process"].lower() in proc.lower():
                    temp.update(paths)
            candidate_paths = candidate_paths.intersection(temp) if candidate_paths else temp

        if parsed["task"]:
            # 精确匹配任务名
            temp = set()
            for cp in self._cache.keys():
                task_name = self._cache[cp].get("task_name", "")
                if parsed["task"].lower() in task_name.lower():
                    temp.add(cp)
            candidate_paths = candidate_paths.intersection(temp) if candidate_paths else temp

    if from_table:
        # 使用输入表索引
        temp = set()
        for table, paths in self._index_by_from_table.items():
            if from_table.lower() in table.lower():
                temp.update(paths)
        candidate_paths = candidate_paths.intersection(temp) if candidate_paths else temp

    if to_table:
        # 使用输出表索引
        temp = set()
        for table, paths in self._index_by_to_table.items():
            if to_table.lower() in table.lower():
                temp.update(paths)
        candidate_paths = candidate_paths.intersection(temp) if candidate_paths else temp

    # 如果没有任何筛选条件，使用全量缓存
    if not candidate_paths:
        candidate_paths = set(self._cache.keys())

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

    return tasks
```

**Step 2: 注册 MCP 工具**

```python
@mcp.tool()
@log_function_info
def datafactory_task_info(
    code_path: Optional[str] = None,
    from_table: Optional[str] = None,
    to_table: Optional[str] = None
) -> str:
    """
    DolphinScheduler 任务详情查询工具 - 基于内存缓存

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
```

**Step 3: 添加缓存刷新工具**

```python
@mcp.tool()
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
```

**Step 4: Commit**

```bash
git commit -m "feat: implement datafactory_task_info with memory cache and add refresh tool"
```

---

### Task 6: 集成测试与调试

#### 6.1 测试用例设计

针对两个工具设计了以下测试案例，覆盖常规功能和边界情况：

##### datafactory_sql_search 测试用例

| # | 测试案例 | pattern | code_path | 其他参数 | 预期结果 |
|---|---------|---------|-----------|----------|---------|
| 1 | 模糊查询项目路径里包含"对公有效户"的代码 | `FROM.*` | `/ds/对公有效户/` | - | 返回包含 FROM 语法的匹配 |
| 2 | 查询 ODS 项目中包含表名的代码 | `JOIN\s+\w+` | `/ds/ODS/` | code_status=已上线 | 返回 JOIN 语句匹配 |
| 3 | 查询 DWD 项目中使用窗口函数的代码 | `OVER\s*\(` | `/ds/DWD/` | - | 返回窗口函数匹配 |
| 4 | 全局搜索包含某张表的代码 | `FROM\s+ods\.\w+` | `/ds/` | - | 跨项目搜索 |
| 5 | 带上下文的匹配（前后各2行） | `INSERT INTO` | `/ds/ODS/` | before=2, after=2 | 返回包含前后文的结果 |
| 6 | 状态过滤：只看已上线 | `SELECT` | `/ds/` | code_status=已上线 | 仅返回已上线任务 |
| 7 | 状态过滤：只看未上线 | `SELECT` | `/ds/` | code_status=未上线 | 仅返回未上线任务 |
| 8 | 分页测试 | `FROM` | `/ds/` | page=1, page_size=10 | 返回第1页，最多10条 |
| 9 | 不存在的匹配 | `XYZNONEXISTENT` | `/ds/ODS/` | - | 空结果，正常返回 |
| 10 | 正则语法错误 | `[invalid(regex` | `/ds/` | - | 返回错误提示 |

##### datafactory_task_info 测试用例

| # | 测试案例 | code_path | from_table | to_table | 预期结果 |
|---|---------|-----------|------------|----------|---------|
| 1 | 按项目查询任务详情 | `/ds/ODS/` | - | - | 返回 ODS 下所有任务 |
| 2 | 按工作流查询 | `/ds/ODS/日报工作流/` | - | - | 返回该工作流下所有任务 |
| 3 | 精确到任务 | `/ds/ODS/日报工作流/用户表同步` | - | - | 返回单个任务详情 |
| 4 | 按输入表查询 | - | ods.user | - | 返回使用该输入表的任务 |
| 5 | 按输出表查询 | - | - | dw.dim_user | 返回产出该表的任务 |
| 6 | 组合过滤：项目+输入表 | `/ds/ODS/` | ods. | - | 双重条件交集 |
| 7 | 无任何筛选条件 | - | - | - | 返回错误提示 |
| 8 | 不存在的路径 | `/ds/不存在的项目/` | - | - | 返回空列表 |
| 9 | 模糊匹配：部分表名 | - | user | - | 返回包含 user 的输入表 |
| 10 | 返回代码行数验证 | `/ds/ODS/` | - | - | 包含"代码行数"字段 |

##### 缓存相关测试用例

| # | 测试案例 | 操作 | 预期结果 |
|---|---------|-----|---------|
| 1 | 首次调用触发懒加载 | 调用任一工具 | 自动加载缓存，日志显示"开始加载血缘数据到缓存" |
| 2 | 手动刷新缓存 | 调用 datafactory_refresh_cache | 缓存重新加载，任务数一致 |
| 3 | 强制刷新缓存 | datafactory_refresh_cache(force=True) | 强制重新加载，清空旧缓存后填充新数据 |
| 4 | 获取缓存统计 | 直接调用 get_cache_stats() | 返回 loaded、task_count、last_load_time 等信息 |

#### 6.2 启动服务测试

```bash
# 启动服务
uvicorn main:app --reload

# 或者后台运行
nohup uvicorn main:app --host 0.0.0.0 --port 8000 > logs/app.log 2>&1 &
```

#### 6.3 执行测试并生成报告

**Step 1: 逐一执行测试用例并记录结果**

针对上述测试用例，执行并记录实际返回：

```bash
# 示例：通过 MCP 客户端调用（假设 MCP 工具可通过 HTTP 调用）
# 测试案例 1: 模糊查询项目路径里包含"对公有效户"的代码
curl -X POST http://localhost:8000/mcp/tools/datafactory_sql_search \
  -H "Content-Type: application/json" \
  -d '{
    "pattern": "FROM.*",
    "code_path": "/ds/对公有效户/"
  }'

# 测试案例 2: 查询任务详情
curl -X POST http://localhost:8000/mcp/tools/datafactory_task_info \
  -H "Content-Type: application/json" \
  -d '{
    "code_path": "/ds/ODS/"
  }'

# 测试缓存刷新
curl -X POST http://localhost:8000/mcp/tools/datafactory_refresh_cache \
  -H "Content-Type: application/json" \
  -d '{"force": true}'
```

**Step 2: 生成测试报告**

测试完成后，整理测试报告，保存至 `docs/testing/` 目录：

```markdown
# DolphinScheduler 代码检索工具 - 测试报告

**测试日期**: 2026-04-01
**测试人员**: xxx
**版本**: v1.0.0

---

## 测试环境

| 环境项 | 值 |
|-------|-----|
| 服务器 | 125.1.192.78 |
| 数据库 | data_factory.metadata_ds_table_lineage |
| 数据规模 | xxx 条任务 |

---

## 测试结果汇总

| 类别 | 通过 | 失败 | 总计 |
|-----|------|------|------|
| datafactory_sql_search | 10 | 0 | 10 |
| datafactory_task_info | 9 | 1 | 10 |
| 缓存相关 | 4 | 0 | 4 |
| **总计** | **23** | **1** | **24** |

---

## 详细测试记录

### datafactory_sql_search

#### 测试案例 1: 模糊查询项目路径里包含"对公有效户"的代码

- **输入**: pattern=`FROM.*`, code_path=`/ds/对公有效户/`
- **预期**: 返回包含 FROM 语法的匹配
- **实际结果**: ✅ 通过
- **返回样本**:
```json
{
  "matches": [
    {
      "code_path": "/ds/对公有效户/企业开户/年报生成/月度汇总",
      "匹配行数": 3,
      "行号": [12, 45, 78],
      "代码片段": "FROM dwd.fact_corp_account\nWHERE account_date >= '${st}'"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 50,
    "total": 1,
    "total_pages": 1
  }
}
```

#### 测试案例 10: 正则语法错误

- **输入**: pattern=`[invalid(regex`
- **预期**: 返回错误提示
- **实际结果**: ✅ 通过
- **返回样本**: `"错误: 正则表达式错误: ..."`

---

### datafactory_task_info

#### 测试案例 7: 无任何筛选条件

- **输入**: 无参数
- **预期**: 返回错误提示"请至少填写一个筛选条件"
- **实际结果**: ❌ 失败
- **问题**: 返回空数组而不是错误提示
- **修复建议**: 需要在方法开头增加校验

---

## 问题清单

| # | 问题描述 | 严重程度 | 状态 |
|---|---------|---------|------|
| 1 | datafactory_task_info 无参数时应返回错误，但返回空数组 | 高 | 待修复 |

---

## 结论

测试覆盖率：xx%

整体评价：基本满足需求，发现 1 个 bug，需要修复后重新测试。

---
```

**Step 3: 修复发现的问题**

根据测试报告中列出的问题进行修复，然后重新执行对应测试。

**Step 4: 最终Commit**

```bash
git add docs/testing/
git commit -m "test: add test report for ds_code_search tools"
```

---

## 完成标准

- [ ] 程序启动时自动加载缓存（或首次调用时懒加载）
- [ ] 两个 MCP 工具正常注册并响应请求（从内存缓存查询）
- [ ] 正则匹配、超时控制正常工作
- [ ] 分页、状态过滤、路径过滤功能正常
- [ ] 边界情况（空结果、语法错误、无参调用）有妥善处理
- [ ] 日志记录正常（关键步骤 INFO，异常 ERROR）
- [ ] 手动刷新缓存功能正常工作
- [ ] 定时（8小时）自动刷新缓存生效
- [ ] 使用真实数据完成功能验证

---

## 预计工作量

| 任务 | 预估时间 |
|------|---------|
| Task 0: 配置添加 | 10 分钟 |
| Task 1: 模块创建（缓存架构） | 20 分钟 |
| Task 2: 解析方法 | 10 分钟（在原版本已完成） |
| Task 3: 状态判定 | 5 分钟（在原版本已完成） |
| Task 4: sql_search（缓存版） | 40 分钟 |
| Task 5: task_info（缓存版） | 30 分钟 |
| Task 6: 联调测试 | 30 分钟 |
| **总计** | **~2.5 小时** |

---

## 附录：数据结构示例

### 缓存对象结构
```python
{
    "/ds/ODS/日报工作流/用户表同步": {
        "project_name": "ODS",
        "process_name": "日报工作流",
        "task_name": "用户表同步",
        "process_status": "1",
        "task_status": "1",
        "lineage_type": "SQL",
        "from_database_table": ["ods.user_table"],
        "to_database_table": ["dw.dim_user"],
        "sql_code": "SELECT ...",  # 完整代码
        "sql_lines": ["SELECT ...", "FROM ...", ...],  # 按行拆分
        "任务状态": "已上线"
    }
}
```

### 索引结构
```python
_index_by_project = {
    "ODS": ["/ds/ODS/日报工作流/用户表同步", ...],
    "DWD": [...]
}

_index_by_from_table = {
    "ods.user_table": ["/ds/ODS/日报工作流/用户表同步", ...],
    "dwd.order": [...]
}
```