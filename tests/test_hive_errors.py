import ast
from pathlib import Path
import unittest

from utils.hive_errors import normalize_hive_error


class HiveErrorNormalizationTests(unittest.TestCase):
    def test_normalizes_spark_missing_column_and_removes_plan(self):
        error = r'''TExecuteStatementResp(status=TStatus(statusCode=3, infoMessages=["*org.apache.hive.service.cli.HiveSQLException:Error running query: org.apache.spark.sql.AnalysisException: cannot resolve '`DATA_DT`' given input columns: [spark_catalog.demo.orders.acct_index, spark_catalog.demo.orders.created_at, spark_catalog.demo.orders.order_id]; line 1 pos 7;\n'Project ['DATA_DT]\n+- SubqueryAlias spark_catalog.demo.orders"] , errorMessage="Error running query: org.apache.spark.sql.AnalysisException: cannot resolve '`DATA_DT`' given input columns: [spark_catalog.demo.orders.acct_index, spark_catalog.demo.orders.created_at, spark_catalog.demo.orders.order_id]; line 1 pos 7;\n'Project ['DATA_DT]\n+- SubqueryAlias spark_catalog.demo.orders\n+- Relation[order_id#14L,acct_index#15,created_at#16] parquet"), operationHandle=None)'''

        result = normalize_hive_error(error)

        self.assertEqual(result["category"], "missing_column")
        self.assertEqual(
            result["message"],
            "列 `DATA_DT` 不存在。可用列：acct_index, created_at, order_id",
        )
        self.assertNotIn("Project", result["message"])
        self.assertNotIn("TExecuteStatementResp", result["message"])

    def test_normalizes_missing_relation_without_guessing_database_or_table(self):
        error = (
            'TExecuteStatementResp(status=TStatus(statusCode=3, '
            'errorMessage="Error running query: '
            'org.apache.spark.sql.AnalysisException: '
            'Table or view not found: missing_db.orders; line 1 pos 14"), '
            'operationHandle=None)'
        )

        result = normalize_hive_error(error)

        self.assertEqual(result["category"], "relation_not_found")
        self.assertEqual(
            result["message"],
            "表或视图 `missing_db.orders` 不存在。请检查库名、表名和当前连接权限。",
        )

    def test_falls_back_to_first_info_message_when_error_message_is_absent(self):
        error = r'''TExecuteStatementResp(status=TStatus(StatusCode=3,infoMessages=["*org.apache.hive.service.cli.HiveSQLException: Error running query: org.apache.spark.sql.AnalysisException: failed to resolve the query plan: invalid expression", "org.apache.spark.sql.hive.thriftserver.SparkExecuteStatementOperation:run:SparkExecuteStatementOperation.scala:250"]))'''

        result = normalize_hive_error(error)

        self.assertEqual(result["category"], "analysis_error")
        self.assertEqual(
            result["message"],
            "Hive 查询分析失败：failed to resolve the query plan: invalid expression",
        )

    def test_normalizes_parser_error_to_one_line(self):
        error = r'''TExecuteStatementResp(status=TStatus(statusCode=3, errorMessage="Error running query: org.apache.spark.sql.catalyst.parser.ParseException: \nmismatched input '.' expecting {<EOF>, ';'}(line 1, pos 16)\n\n== SQL ==\nSELECT FROM demo.orders"), operationHandle=None)'''

        result = normalize_hive_error(error)

        self.assertEqual(result["category"], "syntax_error")
        self.assertEqual(
            result["message"],
            "SQL 语法错误：mismatched input '.' expecting {<EOF>, ';'}(line 1, pos 16)",
        )

    def test_unknown_error_is_bounded_and_keeps_a_safe_fallback(self):
        result = normalize_hive_error("java.lang.RuntimeException: " + "x" * 2000, max_length=120)

        self.assertEqual(result["category"], "unknown")
        self.assertLessEqual(len(result["message"]), 120)
        self.assertTrue(result["message"].startswith("Hive 查询失败："))

    def test_query_data_calls_normalizer_before_returning_exception_message(self):
        source_path = Path(__file__).resolve().parents[1] / "tools" / "hive_query.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        query_data = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "query_data"
        )

        called_names = {
            node.func.id
            for node in ast.walk(query_data)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("normalize_hive_error", called_names)


if __name__ == "__main__":
    unittest.main()
