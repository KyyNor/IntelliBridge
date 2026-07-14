import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BlockingRouteTests(unittest.TestCase):
    def test_blocking_rest_handlers_are_sync_fastapi_routes(self):
        expected = {
            "tools/hive_query.py": {
                "describe_table",
                "query_hive_data",
                "list_databases",
                "list_tables",
            },
            "tools/mysql_query.py": {
                "list_databases",
                "search_tables",
                "describe_table",
                "query_mysql_data",
            },
            "tools/agent_browser.py": {"execute_agent_browser"},
            "tools/fine_report_tools.py": {"api_sample", "api_download", "api_paginated"},
        }

        for relative_path, function_names in expected.items():
            tree = ast.parse(
                (ROOT / relative_path).read_text(encoding="utf-8"),
                filename=relative_path,
            )
            functions = {
                node.name: node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in function_names
            }
            self.assertEqual(set(functions), function_names)
            for name, node in functions.items():
                self.assertIsInstance(
                    node,
                    ast.FunctionDef,
                    f"{relative_path}:{name} must run in FastAPI's worker thread",
                )


if __name__ == "__main__":
    unittest.main()
