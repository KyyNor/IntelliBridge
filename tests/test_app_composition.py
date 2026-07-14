import ast
from pathlib import Path
import unittest


MAIN = Path(__file__).resolve().parents[1] / "main.py"


class AppCompositionTests(unittest.TestCase):
    def test_served_app_registers_fine_report_and_timeout_lifecycle(self):
        source = MAIN.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MAIN))
        functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertIn("health_ready", functions)
        self.assertIn("shutdown_resources", functions)
        self.assertIn("app.include_router(fr_router)", source)
        self.assertIn("combined_app.add_middleware(", source)
        self.assertIn("RequestTimeoutMiddleware", source)
        self.assertIn("CORSMiddleware", source)


if __name__ == "__main__":
    unittest.main()
