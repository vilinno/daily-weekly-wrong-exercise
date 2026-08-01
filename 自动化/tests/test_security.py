import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


AUTOMATION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUTOMATION_DIR))

from wrong_questions.foundation import ROOT, Source  # noqa: E402
from wrong_questions.audit import audit_markdown_references, audit_repository, classify_reference  # noqa: E402
from wrong_questions.question_index import assign_question_ids  # noqa: E402
from wrong_questions.repo_paths import read_repo_image, resolve_repo_file, resolve_repo_image  # noqa: E402
from wrong_questions.report_io import write_report_pair  # noqa: E402
from wrong_questions.run_metadata import run_metadata_block, run_metadata_payload  # noqa: E402


class SecurityAndPipelineTests(unittest.TestCase):
    def test_audit_classifies_external_references(self):
        self.assertEqual(classify_reference("https://example.test/a.png"), "remote")
        self.assertEqual(classify_reference("C:/outside/a.png"), "absolute")
        self.assertEqual(classify_reference("assets/a.png"), "relative")

    def test_audit_fixture_reports_broken_and_external_images(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            note = Path(directory) / "fixture.md"
            findings = audit_markdown_references(
                note,
                "![](https://example.test/a.png)\n![](C:/outside/a.png)\n![](missing.png)\n",
            )
            self.assertEqual(
                {finding.code for finding in findings},
                {"markdown.image_remote", "markdown.image_absolute", "markdown.image_broken"},
            )

    def test_audit_ignores_inline_code_image_examples(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            note = Path(directory) / "fixture.md"
            findings = audit_markdown_references(
                note,
                "示例：`![[文件名.png]]`，以及 `![说明](missing.png)`。\n",
            )
            self.assertEqual(findings, [])

    def test_audit_does_not_treat_missing_image_hash_as_duplicate(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            image_dir = root / "数学" / "assets"
            image_dir.mkdir(parents=True)
            (image_dir / "题图.png").write_bytes(b"not a real image")
            index_path = root / "索引" / "题目索引.json"
            index_path.parent.mkdir()
            index_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "questions": [
                            {"question_id": "q_without_image_1", "image_sha256": None},
                            {"question_id": "q_without_image_2", "image_sha256": None},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = audit_repository(
                {"subjects": {"数学": "数学"}, "reports": {}},
                root=root,
                index_path=index_path,
            )
            self.assertNotIn(
                "index.duplicate_image_identity",
                {finding.code for finding in result.findings},
            )

    def test_repo_file_rejects_external_references(self):
        for value in ("https://example.test/a.png", "C:/outside/a.png", "../outside.txt"):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    resolve_repo_file(value)

    def test_repo_image_rejects_absolute_and_remote_references(self):
        for value in ("https://example.test/a.png", "C:/outside/a.png"):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    resolve_repo_image(value)

    def test_image_reader_rejects_disguised_image(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / "伪装.png"
            path.write_bytes(b"not an image")
            with self.assertRaises(Exception):
                read_repo_image(path)

    def test_question_id_is_stable_when_path_changes(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            first = Path(directory) / "first.png"
            second = Path(directory) / "second.png"
            first.write_bytes(b"same source bytes")
            second.write_bytes(first.read_bytes())
            left = Source("S001", "测试", image_path=first)
            right = Source("S002", "测试", image_path=second)
            assign_question_ids([left, right])
            self.assertEqual(left.question_id, right.question_id)

    def test_report_pair_is_written_as_a_pair(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            first = Path(directory) / "题目.md"
            second = Path(directory) / "答案.md"
            write_report_pair(first, "题目\n", second, "答案\n", True)
            self.assertEqual(first.read_text(encoding="utf-8"), "题目\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "答案\n")

    def test_run_metadata_has_machine_readable_issue_objects(self):
        config = {
            "git": {"tracked_ref": "HEAD"},
            "ai": {"default_model": "fixture-model"},
            "pipeline": {"prompt_version": "fixture-v1"},
        }
        payload = run_metadata_payload(
            kind="fixture",
            status="needs_review",
            config=config,
            question_ids=["q_fixture"],
            issues=["来源待确认"],
            run_id="fixture-run",
        )
        self.assertEqual(payload["run_id"], "fixture-run")
        self.assertEqual(payload["issues"][0]["code"], "pipeline.issue")
        self.assertEqual(
            {"code", "severity", "message"},
            set(payload["issues"][0]),
        )
        block = run_metadata_block(
            kind="fixture", status="needs_review", config=config,
            question_ids=["q_fixture"], issues=["来源待确认"], run_id="fixture-run",
        )
        self.assertIn("```json", block)
        machine_value = block.split("```json", 1)[1].split("```", 1)[0].strip()
        self.assertEqual(json.loads(machine_value)["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
