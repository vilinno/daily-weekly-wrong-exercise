import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


AUTOMATION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUTOMATION_DIR))

import main  # noqa: E402


class WorkflowUnitTests(unittest.TestCase):
    def test_scheduled_daily_date_uses_beijing_boundary(self):
        before = datetime(2026, 7, 21, 22, 29, tzinfo=main.BEIJING)
        at_boundary = datetime(2026, 7, 21, 22, 30, tzinfo=main.BEIJING)
        self.assertEqual(main.scheduled_daily_date("22:30", before), date(2026, 7, 20))
        self.assertEqual(
            main.scheduled_daily_date("22:30", at_boundary), date(2026, 7, 21)
        )

    def test_scheduled_weekly_end_uses_previous_boundary_when_late(self):
        before = datetime(2026, 7, 26, 7, 59, tzinfo=main.BEIJING)
        at_boundary = datetime(2026, 7, 26, 8, 0, tzinfo=main.BEIJING)
        self.assertEqual(
            main.scheduled_weekly_end("Sunday", "08:00", before),
            datetime(2026, 7, 19, 8, 0, tzinfo=main.BEIJING),
        )
        self.assertEqual(
            main.scheduled_weekly_end("Sunday", "08:00", at_boundary),
            datetime(2026, 7, 26, 8, 0, tzinfo=main.BEIJING),
        )

    def test_dirty_note_is_not_used_as_source(self):
        changed = {"数学/高等数学/多元微分.md": {"M"}}
        bundle = main.build_subject_bundle(
            "数学",
            changed,
            "数学",
            {"数学/高等数学/多元微分.md"},
        )
        self.assertEqual(bundle.sources, [])
        self.assertTrue(any("未提交" in problem for problem in bundle.problems))

    def test_validate_config_requires_beijing_timezone(self):
        with self.assertRaises(main.WorkflowError):
            main.validate_config({"timezone": "UTC"})

    def test_commit_message_rules(self):
        self.assertTrue(main.is_allowed_message("daily: 2026-07-21"))
        self.assertTrue(main.is_allowed_message("docs: 更新说明"))
        self.assertFalse(main.is_allowed_message("daily 2026-07-21"))

    def test_commit_date_parser(self):
        commits = [
            main.Commit(
                "abc123456789",
                main.parse_git_datetime("2026-07-21T14:30:00+08:00"),
                "daily: 2026-07-21",
            )
        ]
        self.assertEqual(main.commits_for_daily(commits, date(2026, 7, 21)), commits)

    def test_changed_paths_request_unquoted_non_ascii_paths(self):
        commit = main.Commit(
            "abc123456789",
            main.parse_git_datetime("2026-07-21T14:30:00+08:00"),
            "daily: 2026-07-21",
        )
        git_output = "M\t数学/高等数学/无穷级数.md\n"
        with patch.object(main, "run_git", return_value=git_output) as run_git:
            self.assertEqual(
                main.changed_paths_for_commit(commit),
                [("M", "数学/高等数学/无穷级数.md")],
            )
        run_git.assert_called_once_with(
            "-c",
            "core.quotePath=false",
            "show",
            "--format=",
            "--name-status",
            "--find-renames",
            "--find-copies",
            commit.sha,
        )

    def test_parse_image_target(self):
        self.assertEqual(main.parse_image_target("<assets/题图.png>"), "assets/题图.png")
        self.assertEqual(main.parse_image_target("assets/题图.png \"题目\""), "assets/题图.png")

    def test_parse_obsidian_image_target_ignores_size(self):
        self.assertEqual(
            main.parse_obsidian_image_target("assets/题图.png|640x480"),
            "assets/题图.png",
        )

    def test_parse_diff_new_line_ranges(self):
        diff = """@@ -4,0 +5,3 @@
+### 新题
+#### 题目
+![[新题.png]]
@@ -20 +23 @@
-旧内容
+新内容
@@ -30,2 +32,0 @@
"""
        self.assertEqual(
            main.parse_diff_new_line_ranges(diff),
            [(5, 7), (23, 23)],
        )

    def test_incremental_note_sources_exclude_historical_question(self):
        with TemporaryDirectory(dir=main.ROOT) as temporary_directory:
            temporary_root = Path(temporary_directory)
            note_path = temporary_root / "示例.md"
            old_image = temporary_root / "旧题.png"
            new_image = temporary_root / "新题.png"
            old_image.write_bytes(b"old")
            new_image.write_bytes(b"new")
            content = """## 矩阵

### 旧题
#### 题目
![[旧题.png]]
#### 总结
旧题总结

### 新题
#### 题目
![[新题.png]]
#### 总结
新题总结
"""
            note_path.write_text(content, encoding="utf-8")
            new_image_line = content.splitlines().index("![[新题.png]]") + 1

            sources, problems = main.incremental_note_sources(
                note_path,
                "数学",
                content,
                [(new_image_line, new_image_line)],
            )

        self.assertEqual(problems, [])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].image_path, new_image.resolve())
        self.assertEqual(sources[0].change_kind, "新增题目")
        self.assertIn("新题总结", sources[0].context)
        self.assertNotIn("旧题总结", sources[0].context)

    def test_incremental_note_sources_keep_text_only_update_with_existing_image(self):
        with TemporaryDirectory(dir=main.ROOT) as temporary_directory:
            temporary_root = Path(temporary_directory)
            note_path = temporary_root / "示例.md"
            image_path = temporary_root / "题图.png"
            image_path.write_bytes(b"image")
            content = """## 级数
### 旧题
#### 题目
![[题图.png]]
#### 总结
原有总结
新增复盘说明
"""
            note_path.write_text(content, encoding="utf-8")
            update_line = content.splitlines().index("新增复盘说明") + 1

            sources, problems = main.incremental_note_sources(
                note_path,
                "数学",
                content,
                [(update_line, update_line)],
            )

        self.assertEqual(problems, [])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].image_path, image_path.resolve())
        self.assertEqual(sources[0].change_kind, "笔记新增/修改（关联已有题图）")
        self.assertIn("新增复盘说明", sources[0].context)

    def test_incremental_note_sources_ignore_whitespace_only_change(self):
        with TemporaryDirectory(dir=main.ROOT) as temporary_directory:
            temporary_root = Path(temporary_directory)
            note_path = temporary_root / "示例.md"
            note_path.write_text("## 级数\n\n", encoding="utf-8")

            sources, problems = main.incremental_note_sources(
                note_path,
                "数学",
                note_path.read_text(encoding="utf-8"),
                [(2, 2)],
            )

        self.assertEqual(sources, [])
        self.assertEqual(problems, [])

    def test_iter_image_targets_supports_both_markdown_syntaxes(self):
        line = "![标准图片](assets/a.png) ![[b.png|300]] ![[说明.md]]"
        self.assertEqual(
            list(main.iter_image_targets(line)),
            ["assets/a.png", "b.png"],
        )

    def test_parse_note_images_resolves_obsidian_embed_to_note(self):
        with TemporaryDirectory(dir=main.ROOT) as temporary_directory:
            temporary_root = Path(temporary_directory)
            note_path = temporary_root / "笔记" / "示例.md"
            image_path = temporary_root / "assets" / "题图.png"
            note_path.parent.mkdir(parents=True)
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"not-a-real-image")
            note_path.write_text(
                "# 示例\n\n## 题目\n\n![[题图.png|640x480]]\n",
                encoding="utf-8",
            )

            _, sources, problems = main.parse_note_images(note_path, "数学")

        self.assertEqual(problems, [])
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].raw_image_ref, "题图.png")
        self.assertEqual(sources[0].note_path, note_path.resolve())
        self.assertEqual(sources[0].image_path, image_path.resolve())
        self.assertEqual(sources[0].headings, ["示例", "题目"])

    def test_split_weekly_output(self):
        value = """
        <SUMMARY>
        ## 过去一周总结
        - 方法：放缩（来源：S001）
        </SUMMARY>
        <TEST>
        ## 测试题
        1. 原题：...
        </TEST>
        <ANSWER>
        ## 答案与核验
        1. 核验依据：S001
        </ANSWER>
        """
        summary, test, answer = main.split_weekly_output(value)
        self.assertIn("过去一周总结", summary)
        self.assertIn("测试题", test)
        self.assertIn("答案与核验", answer)

    def test_chat_endpoint_from_base_url(self):
        old_base = main.os.environ.get("OPENAI_BASE_URL")
        old_explicit = main.os.environ.get("OPENAI_CHAT_COMPLETIONS_URL")
        try:
            main.os.environ["OPENAI_BASE_URL"] = "https://example.test"
            main.os.environ.pop("OPENAI_CHAT_COMPLETIONS_URL", None)
            self.assertEqual(
                main.chat_completions_endpoint({}),
                "https://example.test/v1/chat/completions",
            )
            main.os.environ["OPENAI_CHAT_COMPLETIONS_URL"] = "https://example.test/custom"
            self.assertEqual(
                main.chat_completions_endpoint({}),
                "https://example.test/custom",
            )
        finally:
            if old_base is None:
                main.os.environ.pop("OPENAI_BASE_URL", None)
            else:
                main.os.environ["OPENAI_BASE_URL"] = old_base
            if old_explicit is None:
                main.os.environ.pop("OPENAI_CHAT_COMPLETIONS_URL", None)
            else:
                main.os.environ["OPENAI_CHAT_COMPLETIONS_URL"] = old_explicit

    def test_chat_response_parser(self):
        payload = {"choices": [{"message": {"content": "测试成功"}}]}
        self.assertEqual(main.extract_chat_response_text(payload), "测试成功")

    def test_source_id_links_point_to_image(self):
        image = main.ROOT / "数学" / "高等数学" / "assets" / "示例.png"
        report = main.ROOT / "报告" / "每周" / "周测.md"
        bundle = main.SubjectBundle(
            subject="数学",
            changed_paths=[],
            sources=[
                main.Source(
                    source_id="S001",
                    subject="数学",
                    image_path=image,
                    headings=["级数", "放缩"],
                )
            ],
            problems=[],
            note_texts={},
        )
        linked = main.link_source_ids("方法说明（来源：S001）", bundle, report)
        self.assertIn(
            "[[数学/高等数学/assets/示例.png|S001]]",
            linked,
        )
        self.assertEqual(
            main.link_source_ids(linked, bundle, report),
            linked,
        )

    def test_obsidian_link_uses_vault_relative_target(self):
        self.assertEqual(
            main.obsidian_link("打开笔记", "数学/高等数学/无穷级数.md"),
            "[[数学/高等数学/无穷级数.md|打开笔记]]",
        )
        self.assertEqual(
            main.obsidian_link(None, "数学/高等数学/无穷级数.md"),
            "[[数学/高等数学/无穷级数.md]]",
        )

    def test_source_index_uses_obsidian_links_for_image_and_note(self):
        image = main.ROOT / "数学" / "高等数学" / "assets" / "无穷级数-作差证收敛.png"
        note = main.ROOT / "数学" / "高等数学" / "无穷级数.md"
        report = main.ROOT / "报告" / "每日" / "日报-测试.md"
        bundle = main.SubjectBundle(
            subject="数学",
            changed_paths=[],
            sources=[
                main.Source(
                    source_id="S001",
                    subject="数学",
                    image_path=image,
                    note_path=note,
                    headings=["级数", "作差"],
                )
            ],
            problems=[],
            note_texts={},
        )

        index = main.source_index_markdown(bundle, report)

        self.assertIn(
            "[[数学/高等数学/assets/无穷级数-作差证收敛.png]]",
            index,
        )
        self.assertIn(
            "[[数学/高等数学/无穷级数.md]]",
            index,
        )

    def test_source_id_link_in_table_does_not_add_alias_pipe(self):
        image = main.ROOT / "数学" / "高等数学" / "assets" / "示例.png"
        report = main.ROOT / "报告" / "每周" / "周测.md"
        bundle = main.SubjectBundle(
            subject="数学",
            changed_paths=[],
            sources=[
                main.Source(
                    source_id="S001",
                    subject="数学",
                    image_path=image,
                )
            ],
            problems=[],
            note_texts={},
        )

        linked = main.link_source_ids("| 来源 | S001 |\n|---|---|", bundle, report)

        self.assertEqual(linked, "| 来源 | [[数学/高等数学/assets/示例.png]] |\n|---|---|")


if __name__ == "__main__":
    unittest.main()
