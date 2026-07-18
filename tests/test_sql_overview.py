"""tests for utils.sql_overview and the ds_code_search fallback path.

Covers:
- summarize_sql: INSERT+CTE, multi-statement, nested CTE, CREATE TEMPORARY,
  dirty SQL (parse_error), CTE line numbers, CTE-reference exclusion.
- search_sql_codes fallback: empty pattern and oversized result both trigger
  the overview+hint response (no original SQL returned).
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
# search_sql_codes 降级路径测试（注入假缓存，不依赖数据库）
# ---------------------------------------------------------------------------

class SearchFallbackTests(unittest.TestCase):
    """空 pattern 或返回过大 → 返回概览 + 提示，不返原 SQL。"""

    def _make_instance_with_cache(self, tasks):
        """构造一个 DataFactoryCodeSearch 实例，缓存已注入 tasks。

        tasks: {code_path: {sql_code, 任务状态, from_database_table, to_database_table, lineage_type}}
        """
        from tools.ds_code_search import DataFactoryCodeSearch

        inst = DataFactoryCodeSearch()
        cache = {}
        for path, t in tasks.items():
            cache[path] = {
                "sql_code": t.get("sql_code", ""),
                "sql_lines": t.get("sql_code", "").split("\n"),
                "任务状态": t.get("任务状态", "已上线"),
                "lineage_type": t.get("lineage_type", "SQL"),
                "from_database_table": t.get("from_database_table", []),
                "to_database_table": t.get("to_database_table", []),
                "project_name": "proj",
                "process_name": "proc",
                "task_name": path.rsplit("/", 1)[-1],
                "process_status": "1",
                "task_status": "1",
            }
        inst._snapshot.replace((cache, {}, {}))
        inst._cache_loaded = True  # 跳过 load_cache
        return inst

    def test_empty_pattern_triggers_fallback_with_hint(self):
        inst = self._make_instance_with_cache({
            "/ds/p/proc/taskA": {
                "sql_code": "INSERT INTO dws.a SELECT * FROM ods.x",
            }
        })
        result = inst.search_sql_codes(pattern="", code_path="/ds/")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["fallback_reason"], "empty_pattern")
        self.assertIn("更精确", result["hint"])
        # 概览里有这个任务，且不含 SQL 原文
        self.assertEqual(result["overview_count"], 1)
        ov = result["overviews"][0]
        self.assertEqual(ov["code_path"], "/ds/p/proc/taskA")
        self.assertNotIn("sql_code", ov)
        self.assertNotIn("代码片段", ov)
        # 但有结构信息
        self.assertTrue(ov["parse_ok"])
        self.assertEqual(ov["statement_count"], 1)

    def test_whitespace_only_pattern_triggers_fallback(self):
        inst = self._make_instance_with_cache({
            "/ds/p/proc/taskA": {"sql_code": "SELECT 1"},
        })
        result = inst.search_sql_codes(pattern="   ", code_path="/ds/")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["fallback_reason"], "empty_pattern")

    def test_oversized_result_triggers_fallback(self):
        # 构造一个会命中很多行的大 SQL（空格分隔，让每个 token 都是匹配行）
        big_lines = "\n".join(f"SELECT col_{i} FROM ods.big" for i in range(500))
        big_sql = f"INSERT INTO dws.big\n{big_lines}"
        inst = self._make_instance_with_cache({
            "/ds/p/proc/bigTask": {"sql_code": big_sql},
        })
        # pattern 命中每行（SELECT 出现在每行）→ 返回内容超阈值
        result = inst.search_sql_codes(pattern="SELECT", code_path="/ds/")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["fallback_reason"], "result_too_large")
        # 不返原 SQL
        blob = str(result)
        self.assertNotIn("col_499", blob)  # 原 SQL 里的内容不该出现

    def test_normal_pattern_does_not_trigger_fallback(self):
        inst = self._make_instance_with_cache({
            "/ds/p/proc/taskA": {"sql_code": "INSERT INTO dws.a SELECT id FROM ods.x"},
        })
        result = inst.search_sql_codes(pattern="ods.x", code_path="/ds/")
        self.assertNotIn("fallback", result)
        self.assertIn("matches", result)
        self.assertEqual(len(result["matches"]), 1)

    def test_fallback_overview_includes_cte_lines(self):
        sql = (
            "INSERT INTO dws.a\n"
            "WITH cte_x AS (\n"
            "    SELECT id FROM ods.src\n"
            ")\n"
            "SELECT * FROM cte_x"
        )
        inst = self._make_instance_with_cache({
            "/ds/p/proc/taskCte": {"sql_code": sql},
        })
        result = inst.search_sql_codes(pattern="", code_path="/ds/")
        ov = result["overviews"][0]
        stmt = ov["statements"][0]
        self.assertEqual(stmt["ctes"], [{"name": "cte_x", "line": 2}])


# ---------------------------------------------------------------------------
# get_sql_overview 方法测试
# ---------------------------------------------------------------------------

class GetSqlOverviewTests(unittest.TestCase):
    """get_sql_overview：路径+状态过滤 + 概览生成。"""

    def _make_instance_with_cache(self, tasks):
        from tools.ds_code_search import DataFactoryCodeSearch
        inst = DataFactoryCodeSearch()
        cache = {}
        for path, t in tasks.items():
            cache[path] = {
                "sql_code": t.get("sql_code", ""),
                "sql_lines": t.get("sql_code", "").split("\n"),
                "任务状态": t.get("任务状态", "已上线"),
                "lineage_type": t.get("lineage_type", "SQL"),
                "from_database_table": t.get("from_database_table", []),
                "to_database_table": t.get("to_database_table", []),
            }
        inst._snapshot.replace((cache, {}, {}))
        inst._cache_loaded = True
        return inst

    def test_overview_filters_by_path_and_status(self):
        inst = self._make_instance_with_cache({
            "/ds/projA/proc/task1": {
                "sql_code": "INSERT INTO dws.a SELECT * FROM ods.x",
                "任务状态": "已上线",
            },
            "/ds/projB/proc/task2": {
                "sql_code": "INSERT INTO dws.b SELECT * FROM ods.y",
                "任务状态": "未上线",
            },
        })
        # 只看 projA 已上线
        r = inst.get_sql_overview(code_path="/ds/projA/", code_status="已上线")
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["overviews"][0]["code_path"], "/ds/projA/proc/task1")

    def test_overview_does_not_return_sql_raw(self):
        inst = self._make_instance_with_cache({
            "/ds/p/proc/t": {
                "sql_code": "INSERT INTO dws.a SELECT * FROM ods.secret_table_xyz",
            }
        })
        r = inst.get_sql_overview(code_path="/ds/")
        blob = str(r)
        # 原 SQL 的表名不该出现在概览里（secret_table_xyz 是输入表，会出现在 input_tables，
        # 但这是结构信息不是原文）。这里验证「代码片段」字段不存在。
        self.assertNotIn("代码片段", r["overviews"][0])
        self.assertNotIn("sql_code", r["overviews"][0])


if __name__ == "__main__":
    unittest.main()
