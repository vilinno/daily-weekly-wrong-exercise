import ast
import sys
import unittest
from pathlib import Path


AUTOMATION_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DIR = AUTOMATION_DIR / "wrong_questions"
sys.path.insert(0, str(AUTOMATION_DIR))

from wrong_questions import correction_workflow  # noqa: E402
from wrong_questions import daily_workflow  # noqa: E402
from wrong_questions import review_workflow  # noqa: E402
from wrong_questions import weekly_workflow  # noqa: E402


class ArchitectureTests(unittest.TestCase):
    def test_main_is_thin_cli_entrypoint(self):
        content = (AUTOMATION_DIR / "main.py").read_text(encoding="utf-8")

        self.assertLessEqual(len(content.splitlines()), 10)
        self.assertIn("from wrong_questions.cli import main", content)

    def test_each_feature_has_independent_workflow_module(self):
        self.assertEqual(daily_workflow.daily_report.__module__, daily_workflow.__name__)
        self.assertEqual(
            weekly_workflow.weekly_reports.__module__, weekly_workflow.__name__
        )
        self.assertEqual(
            review_workflow.review_reports.__module__, review_workflow.__name__
        )
        self.assertEqual(
            correction_workflow.correction_reports.__module__,
            correction_workflow.__name__,
        )

    def test_production_modules_do_not_use_wildcard_imports(self):
        violations = []
        for path in PACKAGE_DIR.glob("*.py"):
            if path.name == "testing_api.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "*" for alias in node.names
                ):
                    violations.append(path.name)

        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
