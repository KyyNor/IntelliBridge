"""tests for utils.sql_overview and the ds_code_search code-reading paths.

Covers:
- summarize_sql: INSERT+CTE, multi-statement, nested CTE, CREATE TEMPORARY,
  dirty SQL (parse_error), CTE line numbers, CTE-reference exclusion.
- search_sql_codes: exact-path regex matching, empty-pattern overview fallback,
  range reads, truncation and regex timeout protection.
"""

import sys
import types
import unittest

# 本地 venv 是精简环境，dbutils（mysql_pool 依赖）未必安装；
# 测试不碰数据库，stub 掉即可，与 tests/test_mysql_pool.py 的做法一致。
dbutils_stub = types.ModuleType("dbutils")
pooled_db_stub = types.ModuleType("dbutils.pooled_db")
pooled_db_stub.PooledDB = object
dbutils_stub.pooled_db = pooled_db_stub
sys.modules.setdefault("dbutils", dbutils_stub)
sys.modules.setdefault("dbutils.pooled_db", pooled_db_stub)

from utils.sql_overview import summarize_sql


# ---------------------------------------------------------------------------
# summarize_sql 单元测试（纯函数，不依赖缓存/数据库）
# ---------------------------------------------------------------------------

class SummarizeSqlCteTests(unittest.TestCase):
    """CTE 提取：名称、行号、嵌套、引用排除。"""

    def test_insert_with_cte_returns_input_output_and_cte_lines(self):
        sql = (
            "INSERT INTO dws.dws_order\n"
            "WITH cte_a AS (\n"
            "    SELECT id FROM ods.x WHERE dt='2026-07-18'\n"
            "),\n"
            "cte_b AS (\n"
            "    SELECT id FROM cte_a\n"
            ")\n"
            "SELECT * FROM cte_b"
        )
        r = summarize_sql(sql)
        self.assertTrue(r["parse_ok"])
        self.assertEqual(r["statement_count"], 1)
        stmt = r["statements"][0]
        self.assertEqual(stmt["type"], "Insert")
        self.assertEqual(stmt["output_table"], "dws.dws_order")
        self.assertEqual(stmt["output_kind"], "INSERT")
        # 输入表只有 ods.x（cte_a / cte_b 是 CTE 引用，必须排除）
        self.assertEqual(stmt["input_tables"], ["ods.x"])
        # CTE 名称 + 行号（cte_a 在第 2 行，cte_b 在第 5 行）
        self.assertEqual(
            stmt["ctes"],
            [{"name": "cte_a", "line": 2}, {"name": "cte_b", "line": 5}],
        )

    def test_nested_cte_inner_and_outer_both_extracted(self):
        sql = (
            "WITH cte_outer AS (\n"
            "    WITH cte_inner AS (SELECT 1 FROM ods.a)\n"
            "    SELECT * FROM cte_inner\n"
            ")\n"
            "SELECT * FROM cte_outer"
        )
        r = summarize_sql(sql)
        stmt = r["statements"][0]
        names = [c["name"] for c in stmt["ctes"]]
        self.assertIn("cte_outer", names)
        self.assertIn("cte_inner", names)
        # 行号都应能定位（相对原文）
        for c in stmt["ctes"]:
            self.assertIsNotNone(c["line"])
        # 物理表只有 ods.a（两个 CTE 引用都排除）
        self.assertEqual(stmt["input_tables"], ["ods.a"])


class SummarizeSqlMultiStmtTests(unittest.TestCase):
    """多语句：分别解析、按序号返回、不串。"""

    def test_multiple_statements_returned_in_order(self):
        sql = (
            "CREATE TEMPORARY TABLE tmp_a AS SELECT id FROM ods.a;\n"
            "INSERT INTO dws.b SELECT id FROM tmp_a;\n"
            "SELECT 1"
        )
        r = summarize_sql(sql)
        self.assertTrue(r["parse_ok"])
        self.assertEqual(r["statement_count"], 3)
        # 序号
        self.assertEqual([s["index"] for s in r["statements"]], [0, 1, 2])
        # 语句0：CREATE TEMPORARY TABLE
        s0 = r["statements"][0]
        self.assertEqual(s0["type"], "Create")
        self.assertEqual(s0["output_table"], "tmp_a")
        self.assertEqual(s0["output_kind"], "CREATE")
        self.assertEqual(s0["input_tables"], ["ods.a"])
        # 语句1：INSERT（tmp_a 是来源，但它是 temp 表 —— 本工具不追踪 temp 表，按字面识别为输入）
        s1 = r["statements"][1]
        self.assertEqual(s1["type"], "Insert")
        self.assertEqual(s1["output_table"], "dws.b")
        # 语句2：纯 SELECT
        s2 = r["statements"][2]
        self.assertEqual(s2["type"], "Select")
        self.assertIsNone(s2["output_table"])


class SummarizeSqlDirtySqlTests(unittest.TestCase):
    """脏 SQL：解析失败标记，不抛异常、不阻断。"""

    def test_syntax_error_marks_parse_error_without_raising(self):
        sql = "SELECT FROM ods.x broken syntax here"
        r = summarize_sql(sql)
        self.assertFalse(r["parse_ok"])
        self.assertEqual(r["statement_count"], 1)
        stmt = r["statements"][0]
        self.assertIsNotNone(stmt["parse_error"])
        self.assertEqual(stmt["input_tables"], [])
        self.assertIsNone(stmt["output_table"])

    def test_empty_sql_returns_not_ok(self):
        r = summarize_sql("")
        self.assertFalse(r["parse_ok"])
        self.assertEqual(r["statement_count"], 0)


class SummarizeSqlEdgeTests(unittest.TestCase):
    """边界：INSERT OVERWRITE、JOIN、派生表。"""

    def test_insert_overwrite_recognized_as_insert(self):
        sql = (
            "INSERT OVERWRITE TABLE dws.r PARTITION (dt='2026-07-18')\n"
            "SELECT a FROM ods.src"
        )
        r = summarize_sql(sql)
        stmt = r["statements"][0]
        self.assertEqual(stmt["output_kind"], "INSERT")
        self.assertEqual(stmt["input_tables"], ["ods.src"])

    def test_join_tables_both_collected(self):
        sql = (
            "SELECT a.id FROM ods.a JOIN ods.b ON a.id = b.id"
        )
        r = summarize_sql(sql)
        stmt = r["statements"][0]
        self.assertEqual(sorted(stmt["input_tables"]), ["ods.a", "ods.b"])

    def test_derived_table_first_source_collected(self):
        sql = (
            "SELECT t.id FROM (SELECT id FROM ods.x) t"
        )
        r = summarize_sql(sql)
        stmt = r["statements"][0]
        # 派生表：取其内部第一个物理表
        self.assertIn("ods.x", stmt["input_tables"])


# ---------------------------------------------------------------------------
# search_sql_codes 测试（注入假缓存，不依赖数据库）
# ---------------------------------------------------------------------------

class SearchSqlTests(unittest.TestCase):
    """覆盖精确路径、正则检索、空 pattern 回退和行范围读取。"""

    def _make_instance_with_cache(self, tasks):
        from tools.ds_code_search import DataFactoryCodeSearch

        inst = DataFactoryCodeSearch()
        cache = {}
        for path, task in tasks.items():
            sql_code = task.get("sql_code", "")
            cache[path] = {
                "sql_code": sql_code,
                "sql_lines": sql_code.split("\n"),
                "任务状态": task.get("任务状态", "已上线"),
                "lineage_type": task.get("lineage_type", "SQL"),
                "from_database_table": task.get("from_database_table", []),
                "to_database_table": task.get("to_database_table", []),
                "project_name": "proj",
                "process_name": "proc",
                "task_name": path.rsplit("/", 1)[-1],
                "process_status": "1",
                "task_status": "1",
            }
        inst._snapshot.replace((cache, {}, {}))
        inst._cache_loaded = True
        return inst

    def test_empty_pattern_returns_overview_with_reason(self):
        path = "/ds/p/proc/taskA"
        inst = self._make_instance_with_cache({
            path: {"sql_code": "INSERT INTO dws.a SELECT * FROM ods.x"},
        })
        result = inst.search_sql_codes(pattern="", code_path=path)
        self.assertTrue(result["fallback"])
        self.assertEqual(result["fallback_reason"], "empty_pattern")
        self.assertIn("因为 pattern 为空", result["message"])
        self.assertIn("更精确", result["message"])
        self.assertEqual(result["overview"]["code_path"], path)
        self.assertTrue(result["overview"]["parse_ok"])
        self.assertNotIn("sql_code", result["overview"])

    def test_whitespace_only_pattern_returns_overview(self):
        path = "/ds/p/proc/taskA"
        inst = self._make_instance_with_cache({
            path: {"sql_code": "SELECT 1"},
        })
        result = inst.search_sql_codes(pattern="   ", code_path=path)
        self.assertTrue(result["fallback"])
        self.assertEqual(result["fallback_reason"], "empty_pattern")

    def test_fuzzy_code_path_is_rejected_by_sql(self):
        inst = self._make_instance_with_cache({
            "/ds/p/proc/taskA": {"sql_code": "SELECT 1"},
        })
        result = inst.search_sql_codes(pattern="SELECT", code_path="/ds/p/")
        self.assertIn("error", result)
        self.assertIn("完整任务路径", result["error"])

    def test_regex_pattern_matches_case_insensitively_with_context(self):
        path = "/ds/p/proc/taskA"
        sql = (
            "INSERT INTO dws.a\n"
            "SELECT id\n"
            "FROM ods.foo\n"
            "JOIN ods.bar ON foo.id = bar.id\n"
            "WHERE dt = '2026-07-20'"
        )
        inst = self._make_instance_with_cache({path: {"sql_code": sql}})
        result = inst.search_sql_codes(
            pattern=r"from\s+ods\.foo",
            code_path=path,
            before=1,
            after=1,
        )
        self.assertNotIn("error", result)
        self.assertEqual(result["matches"][0]["行号"], [3])
        self.assertIn("SELECT id", result["matches"][0]["代码片段"])
        self.assertIn("JOIN ods.bar", result["matches"][0]["代码片段"])

    def test_regex_invalid_pattern_returns_error(self):
        path = "/ds/p/proc/taskA"
        inst = self._make_instance_with_cache({
            path: {"sql_code": "SELECT 1"},
        })
        result = inst.search_sql_codes(pattern="(", code_path=path)
        self.assertIn("error", result)
        self.assertIn("正则表达式错误", result["error"])

    def test_wide_pattern_is_truncated_not_overview_fallback(self):
        path = "/ds/p/proc/bigTask"
        sql = "\n".join(f"SELECT col_{i} FROM ods.big" for i in range(150))
        inst = self._make_instance_with_cache({path: {"sql_code": sql}})
        result = inst.search_sql_codes(pattern="SELECT", code_path=path)
        self.assertNotIn("fallback", result)
        match = result["matches"][0]
        self.assertTrue(match["截断"])
        self.assertEqual(len(match["行号"]), 100)
        self.assertEqual(match["匹配行数"], 150)
        self.assertEqual(match["返回行数"], 100)
        self.assertTrue(match["还有更多"])
        self.assertTrue(match["匹配统计完整"])
        self.assertNotIn("col_149", str(result))

    def test_range_mode_reads_requested_lines_without_pattern_fallback(self):
        path = "/ds/p/proc/taskA"
        sql = "\n".join(f"line_{i}" for i in range(1, 121))
        inst = self._make_instance_with_cache({path: {"sql_code": sql}})
        result = inst.search_sql_codes(
            code_path=path,
            start_line=100,
            end_line=110,
        )
        self.assertNotIn("fallback", result)
        self.assertEqual(result["mode"], "range")
        match = result["matches"][0]
        self.assertEqual(match["起始行"], 100)
        self.assertEqual(match["结束行"], 110)
        self.assertEqual(match["行号"], list(range(100, 111)))
        self.assertEqual(match["代码片段"].splitlines()[0], "line_100")
        self.assertEqual(match["代码片段"].splitlines()[-1], "line_110")

    def test_range_mode_rejects_nonempty_pattern(self):
        path = "/ds/p/proc/taskA"
        inst = self._make_instance_with_cache({
            path: {"sql_code": "line_1\nline_2"},
        })
        result = inst.search_sql_codes(
            pattern="SELECT",
            code_path=path,
            start_line=1,
            end_line=2,
        )
        self.assertIn("error", result)
        self.assertIn("不能同时指定", result["error"])

    def test_range_mode_validates_and_limits_lines(self):
        path = "/ds/p/proc/taskA"
        inst = self._make_instance_with_cache({
            path: {"sql_code": "line_1\nline_2"},
        })
        bad_order = inst.search_sql_codes(
            code_path=path, start_line=2, end_line=1
        )
        self.assertIn("error", bad_order)
        too_many = inst.search_sql_codes(
            code_path=path, start_line=1, end_line=101
        )
        self.assertIn("error", too_many)
        self.assertIn("最多读取 100 行", too_many["error"])

    def test_regex_timeout_is_reported(self):
        path = "/ds/p/proc/slowTask"
        inst = self._make_instance_with_cache({
            path: {"sql_code": "a" * 50000 + "!"},
        })
        inst.REGEX_TOTAL_TIMEOUT = 0.001
        result = inst.search_sql_codes(pattern=r"(a+)+$", code_path=path)
        self.assertTrue(result.get("regex_timeout"))
        self.assertIn("总预算", result["message"])


# ---------------------------------------------------------------------------
# task_info 测试
# ---------------------------------------------------------------------------

class TaskInfoTests(unittest.TestCase):
    def _make_instance_with_cache(self, tasks, from_index=None, to_index=None):
        from tools.ds_code_search import DataFactoryCodeSearch

        inst = DataFactoryCodeSearch()
        cache = {}
        for path, task in tasks.items():
            sql_code = task.get("sql_code", "")
            cache[path] = {
                "sql_code": sql_code,
                "sql_lines": sql_code.split("\n"),
                "任务状态": task.get("任务状态", "已上线"),
                "lineage_type": task.get("lineage_type", "SQL"),
                "from_database_table": task.get("from_database_table", []),
                "to_database_table": task.get("to_database_table", []),
                "from_source": task.get("from_source"),
                "from_host": task.get("from_host"),
                "to_source": task.get("to_source"),
                "to_host": task.get("to_host"),
            }
        inst._snapshot.replace((cache, from_index or {}, to_index or {}))
        inst._cache_loaded = True
        return inst

    def test_returns_sql_overview_instead_of_fixed_prefix(self):
        path = "/ds/p/proc/taskA"
        inst = self._make_instance_with_cache({
            path: {
                "sql_code": "SELECT id FROM ods.x\nWHERE dt = '2026-07-20'",
            }
        })
        result = inst.query_task_info(code_path="taskA")
        self.assertEqual(result["pagination"]["total"], 1)
        self.assertEqual(result["pagination"]["returned"], 1)
        self.assertFalse(result["pagination"]["has_next"])
        task = result["tasks"][0]
        self.assertNotIn("代码", task)
        self.assertIn("sql_overview", task)
        self.assertTrue(task["sql_overview"]["parse_ok"])
        self.assertEqual(task["代码行数"], 2)

    def test_multiple_filters_keep_and_semantics_when_intersection_is_empty(self):
        path_a = "/ds/p/proc/taskA"
        path_b = "/ds/p/proc/taskB"
        inst = self._make_instance_with_cache(
            {
                path_a: {
                    "sql_code": "SELECT 1",
                    "from_database_table": ["ods.x"],
                    "to_database_table": ["dws.a"],
                },
                path_b: {
                    "sql_code": "SELECT 2",
                    "from_database_table": ["ods.x"],
                    "to_database_table": ["dws.b"],
                },
            },
            from_index={"ods.x": [path_a, path_b]},
            to_index={"dws.a": [path_a], "dws.b": [path_b]},
        )
        result = inst.query_task_info(
            code_path="taskA",
            from_table="ods.x",
            to_table="dws.b",
        )
        self.assertEqual(result["tasks"], [])
        self.assertEqual(result["pagination"]["total"], 0)

    def test_whitespace_filters_are_ignored_independently(self):
        path_a = "/ds/p/proc/taskA"
        path_b = "/ds/p/proc/taskB"
        inst = self._make_instance_with_cache(
            {
                path_a: {"sql_code": "SELECT 1", "from_database_table": ["ods.x"]},
                path_b: {"sql_code": "SELECT 2", "from_database_table": ["ods.x"]},
            },
            from_index={"ods.x": [path_a, path_b]},
        )
        result = inst.query_task_info(
            code_path=" ",
            from_table="ods.x",
            to_table=" ",
        )
        self.assertEqual(result["pagination"]["total"], 2)

    def test_all_whitespace_filters_still_require_a_condition(self):
        inst = self._make_instance_with_cache({
            "/ds/p/proc/taskA": {"sql_code": "SELECT 1"},
        })
        result = inst.query_task_info(code_path=" ", from_table="\t", to_table="  ")
        self.assertIn("error", result)

    def test_overview_is_reused_for_repeated_task_info_queries(self):
        path = "/ds/p/proc/taskA"
        inst = self._make_instance_with_cache({
            path: {"sql_code": "SELECT 1"},
        })
        inst.query_task_info(code_path="taskA")
        inst.query_task_info(code_path="taskA")
        self.assertEqual(len(inst._overview_cache), 1)

    def test_paginates_tasks_and_returns_agent_hint(self):
        tasks = {
            f"/ds/p/proc/task_{index}": {
                "sql_code": f"SELECT {index}",
            }
            for index in range(1, 4)
        }
        inst = self._make_instance_with_cache(tasks)

        first_page = inst.query_task_info(
            code_path="/ds/p/proc",
            page=1,
            page_size=2,
        )
        self.assertEqual(first_page["pagination"]["total"], 3)
        self.assertEqual(first_page["pagination"]["returned"], 2)
        self.assertEqual(first_page["pagination"]["total_pages"], 2)
        self.assertTrue(first_page["pagination"]["has_next"])
        self.assertIn("page=2", first_page["hint"])

        second_page = inst.query_task_info(
            code_path="/ds/p/proc",
            page=2,
            page_size=2,
        )
        self.assertEqual(second_page["pagination"]["returned"], 1)
        self.assertFalse(second_page["pagination"]["has_next"])
        self.assertIn("已全部展示", second_page["hint"])

    def test_page_beyond_range_returns_empty_page_hint(self):
        path = "/ds/p/proc/taskA"
        inst = self._make_instance_with_cache({
            path: {"sql_code": "SELECT 1"},
        })
        result = inst.query_task_info(code_path="taskA", page=2, page_size=1)
        self.assertEqual(result["tasks"], [])
        self.assertIn("page=1", result["hint"])


if __name__ == "__main__":
    unittest.main()
