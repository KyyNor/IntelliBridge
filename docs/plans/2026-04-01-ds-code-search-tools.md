# DolphinScheduler 代码检索工具实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 实现两个 MCP 工具：datafactory_sql_search（类 grep 的代码检索）和 datafactory_task_info（任务详情查询），用于在 DolphinScheduler 任务代码中进行正则检索。

**Architecture:** 复用现有 mysql_pool 连接池，在 tools/ 目录下新建 ds_code_search.py 模块，实现 DataFactoryCodeSearch 类，通过 @mcp.tool() 注册两个工具函数。

**Tech Stack:** Python, FastAPI, PyMySQL, regex（带超时控制）, 项目现有工具框架

**关键决策：**
- 正则引擎：使用 `regex` 库（支持超时控制）
- 缓存策略：不使用缓存（每次查询结果独立）
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

### Task 1: 创建数据工厂查询模块结构

**Files:**
- Create: `tools/ds_code_search.py`

**Step 1: 编写初始代码框架**

创建空的类和方法结构：

```python
"""DolphinScheduler 代码检索工具"""

import re
import regex
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
```

**Step 2: 运行验证**

执行 `python -c "from tools.ds_code_search import ds_code_search; print('OK')"` 确认模块导入无误。

**Step 3: Commit**

```bash
git add tools/ds_code_search.py
git commit -m "feat: init ds_code_search module structure"
```

---

### Task 2: 实现 code_path 解析辅助方法

**Files:**
- Modify: `tools/ds_code_search.py`

**Step 1: 编写测试用例**

在 tools 目录下创建测试文件 `tests/tools/test_ds_code_search.py`:

```python
import pytest
import sys
sys.path.insert(0, '/home/data_center/whdenganqi/python_project/intellibridge')

from tools.ds_code_search import DataFactoryCodeSearch

def test_parse_code_path():
    """测试 code_path 解析"""
    searcher = DataFactoryCodeSearch()

    # Test 1: 完整路径
    result = searcher.parse_code_path("/ds/ODS/日报工作流/用户表同步")
    assert result == {
        "project": "ODS",
        "process": "日报工作流",
        "task": "用户表同步"
    }

    # Test 2: 只有项目
    result = searcher.parse_code_path("/ds/ODS/")
    assert result == {
        "project": "ODS",
        "process": None,
        "task": None
    }

    # Test 3: 带trim处理
    result = searcher.parse_code_path("  /ds/ODS/  ")
    assert result["project"] == "ODS"


def test_build_like_pattern():
    """测试 LIKE 模式构建"""
    searcher = DataFactoryCodeSearch()

    assert searcher.build_like_pattern("/ds/ODS/") == "%ODS%"
    assert searcher.build_like_pattern("/ds/ODS/日报") == "%ODS%%日报%"
```

**Step 2: 运行测试，确认失败**

```bash
pytest tools/tests/test_ds_code_search.py::test_parse_code_path -v
```

预期: FAIL (方法未实现)

**Step 3: 实现辅助方法**

在 DataFactoryCodeSearch 类中添加：

```python
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
```

**Step 4: 运行测试验证**

```bash
pytest tools/tests/test_ds_code_search.py::test_parse_code_path -v
pytest tools/tests/test_ds_code_search.py::test_build_like_pattern -v
```

预期: PASS

**Step 5: Commit**

```bash
git add tools/ds_code_search.py tools/tests/test_ds_code_search.py
git commit -m "feat: add code_path parsing methods"
```

---

### Task 3: 实现任务状态判定方法

**Files:**
- Modify: `tools/ds_code_search.py`

**Step 1: 编写测试用例**

```python
def test_determine_task_status():
    """测试任务状态判定"""
    searcher = DataFactoryCodeSearch()

    # 已上线
    assert searcher.determine_task_status("1", "1") == "已上线"
    # 未上线组合
    assert searcher.determine_task_status("1", "0") == "未上线"
    assert searcher.determine_task_status("0", "1") == "未上线"
    assert searcher.determine_task_status("0", "0") == "未上线"
    # 边界
    assert searcher.determine_task_status(None, "1") == "未上线"
    assert searcher.determine_task_status("1", None) == "未上线"
```

**Step 2: 运行测试确认失败**

```bash
pytest tools/tests/test_ds_code_search.py::test_determine_task_status -v
```

预期: FAIL

**Step 3: 实现方法**

```python
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
```

**Step 4: 运行测试验证**

预期: PASS

**Step 5: Commit**

```bash
git commit -m "feat: add task status determination method"
```

---

### Task 4: 实现 datafactory_sql_search 核心功能

**Files:**
- Modify: `tools/ds_code_search.py`

**Step 1: 实现数据库查询逻辑**

在 DataFactoryCodeSearch 类中添加方法：

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
    # 1. 解析 code_path
    parsed = self.parse_code_path(code_path)
    like_pattern = self.build_like_pattern(code_path)

    # 2. 确定状态过滤条件
    status_filter = None
    if code_status == "已上线":
        status_filter = {"process_status": "1", "task_status": "1"}
    elif code_status == "未上线":
        status_filter = {"process_status": "0"}  # 简化处理

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

    # 添加路径过滤
    if like_pattern and like_pattern != '%':
        sql += " AND CONCAT(project_name, IFNULL(process_name,''), IFNULL(task_name,'')) LIKE %s"
        params.append(like_pattern)

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

    # 4. 执行查询
    try:
        with mysql_pool.get_connection("node137_data_factory") as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            results = cursor.fetchall()
    except Exception as e:
        logger.error(f"查询失败: {e}")
        return {"matches": [], "pagination": {...}, "error": str(e)}

    # 5. 内存正则匹配
    matches = []
    compiled_regex = None

    try:
        compiled_regex = regex.compile(pattern, timeout=self.REGEX_TIMEOUT)
    except regex.error as e:
        return {"matches": [], "pagination": {...}, "error": f"正则表达式错误: {e}"}

    for row in results:
        sql_code = row.get('sql_code', '')
        if not sql_code:
            continue

        # 逐行匹配
        lines = sql_code.split('\n')
        for line_num, line in enumerate(lines, start=1):
            try:
                if compiled_regex.search(line):
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
            except regex.Timeout:
                logger.warning(f"正则匹配超时: {row['task_name']}")
                break

    # 6. 分页处理
    total = len(matches)
    total_pages = (total + page_size - 1) // page_size
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
    import json
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

**Step 3: 本地测试（模拟环境）**

由于直接查询数据库可能有各种问题，先构造 Mock 数据进行单元测试，验证核心正则匹配逻辑。

**Step 4: Commit**

```bash
git add tools/ds_code_search.py
git commit -m "feat: implement datafactory_sql_search tool"
```

---

### Task 5: 实现 datafactory_task_info 核心功能

**Files:**
- Modify: `tools/ds_code_search.py`

**Step 1: 添加查询方法**

```python
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
        return {"error": "请至少填写一个筛选条件"}

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
        with mysql_pool.get_connection("node137_data_factory") as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            results = cursor.fetchall()
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
        code_preview = '\n'.join(sql_code.split('\n')[:100])

        task_status = self.determine_task_status(
            str(row.get('process_status')),
            str(row.get('task_status'))
        )

        tasks.append({
            "code_path": f"/ds/{row['project_name']}/{row['process_name']}/{row['task_name']}",
            "任务类型": row.get('lineage_type', 'SQL'),
            "代码": code_preview,
            "代码行数": len(sql_code.split('\n')),
            "任务状态": task_status,
            "输入表清单": from_tables,
            "输出表清单": to_tables
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
    DolphinScheduler 任务详情查询工具

    通过 code_path、输入表或输出表筛选任务，返回任务详情信息。

    Args:
        code_path: 路径筛选，支持精确或模糊匹配（如 /ds/*/*/决策）
        from_table: 输入表名称筛选，支持模糊匹配
        to_table: 输出表名称筛选，支持模糊匹配

    Returns:
        JSON 格式的任务详情列表
    """
    import json
    result = ds_code_search.query_task_info(
        code_path=code_path,
        from_table=from_table,
        to_table=to_table
    )

    if result and isinstance(result, list) and "error" in result[0]:
        return f"错误: {result[0]['error']}"

    return json.dumps(result, ensure_ascii=False, indent=2)
```

**Step 3: Commit**

```bash
git commit -m "feat: implement datafactory_task_info tool"
```

---

### Task 6: 集成测试与调试

**Step 1: 启动服务测试**

```bash
# 启动服务
uvicorn main:app --reload

# 调用工具测试
# 1. 测试 datafactory_sql_search
curl -X POST http://localhost:8000/api/mysql/search_tables \
  -H "Content-Type: application/json" \
  -d '{"pattern": "FROM.*user", "code_path": "/ds/ODS/", "page_size": 10}'

# 2. 测试 datafactory_task_info
curl -X POST http://localhost:8000/api/mysql/describe \
  -H "Content-Type: application/json" \
  -d '{"code_path": "/ds/ODS/"}'
```

**Step 2: 修复发现的问题**

根据实际运行结果修复bug。

**Step 3: Commit**

```bash
git commit -m "fix: debug and polish ds_code_search tools"
```

---

## 完成标准

- [ ] 两个 MCP 工具正常注册并响应请求
- [ ] 正则匹配、超时控制正常工作
- [ ] 分页、状态过滤、路径过滤功能正常
- [ ] 边界情况（空结果、语法错误、无参调用）有妥善处理
- [ ] 日志记录正常（关键步骤 INFO，异常 ERROR）
- [ ] 使用真实数据完成功能验证

---

## 预计工作量

| 任务 | 预估时间 |
|------|---------|
| Task 0: 配置添加 | 10 分钟 |
| Task 1: 模块创建 | 10 分钟 |
| Task 2: 解析方法 | 20 分钟 |
| Task 3: 状态判定 | 10 分钟 |
| Task 4: sql_search | 60 分钟 |
| Task 5: task_info | 40 分钟 |
| Task 6: 联调测试 | 30 分钟 |
| **总计** | **~3 小时** |