import hashlib
import json
import os
import socket
import sys
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory


AUTOMATION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUTOMATION_DIR))

from wrong_questions.foundation import ROOT, Source  # noqa: E402
from wrong_questions.audit import audit_markdown_references, audit_repository, classify_reference  # noqa: E402
from wrong_questions.question_index import assign_question_ids, question_id_for_image  # noqa: E402
from wrong_questions.pipeline_validation import validate_generated_output  # noqa: E402
from wrong_questions.repo_paths import read_repo_image, resolve_repo_file, resolve_repo_image  # noqa: E402
from wrong_questions.report_io import workflow_lock, write_report_pair  # noqa: E402
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

    def test_audit_orphan_image_requires_source_markdown_reference(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            image_dir = root / "数学" / "assets"
            image_dir.mkdir(parents=True)
            image = image_dir / "题图.png"
            image.write_bytes(b"not a real image")
            report_dir = root / "报告" / "每日"
            report_dir.mkdir(parents=True)
            (report_dir / "日报.md").write_text(
                "![](../../数学/assets/题图.png)\n", encoding="utf-8"
            )
            result = audit_repository(
                {
                    "subjects": {"数学": "数学"},
                    "reports": {"daily": "报告/每日"},
                },
                root=root,
                index_path=root / "索引" / "题目索引.json",
            )
            self.assertIn(
                "image.orphan", {finding.code for finding in result.findings}
            )

    def test_audit_question_heading_uses_section_boundary(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            note_dir = root / "数学"
            note_dir.mkdir(parents=True)
            note = note_dir / "专题.md"
            note.write_text("### 题目\n#### 总结\n待填写\n", encoding="utf-8")
            result = audit_repository(
                {"subjects": {"数学": "数学"}, "reports": {}},
                root=root,
                index_path=root / "索引" / "题目索引.json",
            )
            self.assertIn(
                "question.heading_without_source",
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
            problems = []
            assign_question_ids([left, right], problems=problems)
            self.assertIsNone(left.question_id)
            self.assertIsNone(right.question_id)
            self.assertTrue(any("未登记" in problem for problem in problems))

    def test_question_id_is_persistent_index_data_not_hash_derived(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / "题图.png"
            path.write_bytes(b"same source bytes")
            source = Source("S001", "测试", image_path=path)
            index = {
                "schema_version": 2,
                "questions": [
                    {
                        "question_id": "Q-persistent-fixture",
                        "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "path_aliases": [],
                        "identity_key": None,
                    }
                ],
            }
            assign_question_ids([source], index)
            self.assertEqual(source.question_id, "Q-persistent-fixture")
            self.assertEqual(question_id_for_image(path, index), "Q-persistent-fixture")

    def test_question_id_lookup_requires_persisted_index(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / "题图.png"
            path.write_bytes(b"not indexed")
            with self.assertRaises(Exception):
                question_id_for_image(path, {"schema_version": 2, "questions": []})

    def test_validation_does_not_claim_domain_validated_without_independent_check(self):
        result = validate_generated_output(
            generated=True,
            structure_verified=True,
            sources_verified=True,
            domain_verified=False,
        )
        self.assertEqual(result.status, "needs_review")
        self.assertIn("pipeline.validation_domain", {item["code"] for item in result.issues})

    def test_report_pair_is_written_as_a_pair(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            first = Path(directory) / "题目.md"
            second = Path(directory) / "答案.md"
            write_report_pair(first, "题目\n", second, "答案\n", True)
            self.assertEqual(first.read_text(encoding="utf-8"), "题目\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "答案\n")

    def test_workflow_lock_records_recovery_metadata(self):
        with TemporaryDirectory(dir=ROOT) as directory:
            lock_path = Path(directory) / ".workflow.lock"
            with mock.patch("wrong_questions.report_io.LOCK_PATH", lock_path):
                with workflow_lock():
                    metadata = lock_path.read_text(encoding="utf-8")
                    self.assertIn(f"pid={os.getpid()}", metadata)
                    self.assertIn(f"host={socket.gethostname()}", metadata)
                    self.assertIn("started_at=", metadata)
                    self.assertIn("run_id=", metadata)
                self.assertFalse(lock_path.exists())

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
        self.assertEqual(payload["scope_kind"], "snapshot")
        self.assertIsNone(payload["base_commit"])
        self.assertEqual(payload["tip_commit"], payload["snapshot_commit"])
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

    def test_run_metadata_records_explicit_source_range(self):
        payload = run_metadata_payload(
            kind="fixture",
            status="needs_review",
            config={"git": {"tracked_ref": "refs/heads/main"}},
            base_commit="base-fixture",
            tip_commit="tip-fixture",
            source_commits=["tip-fixture", "base-fixture", "tip-fixture"],
            scope_kind="range",
        )
        self.assertEqual(payload["base_commit"], "base-fixture")
        self.assertEqual(payload["tip_commit"], "tip-fixture")
        self.assertEqual(payload["source_commits"], ["tip-fixture", "base-fixture"])
        self.assertIsNone(payload["snapshot_commit"])


if __name__ == "__main__":
    unittest.main()
