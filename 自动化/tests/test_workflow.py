import sys
import unittest
from datetime import date
from pathlib import Path


AUTOMATION_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUTOMATION_DIR))

import main  # noqa: E402


class WorkflowUnitTests(unittest.TestCase):
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

    def test_parse_image_target(self):
        self.assertEqual(main.parse_image_target("<assets/题图.png>"), "assets/题图.png")
        self.assertEqual(main.parse_image_target("assets/题图.png \"题目\""), "assets/题图.png")

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
        self.assertIn("[S001](../../数学/高等数学/assets/示例.png)", linked)


if __name__ == "__main__":
    unittest.main()
