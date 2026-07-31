"""每周测试与答案工作流。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from .foundation import ROOT, WorkflowError
from .git_store import collect_changed_paths, commits_for_week, read_commits, read_uncommitted_paths
from .source_scanner import build_subject_bundle
from .markdown_tools import format_commit_list, source_index_markdown
from .prompts import weekly_prompt
from .ai_client import call_openai
from .ai_output import link_source_ids, source_ids_in, split_weekly_output

from .report_io import write_or_preview_report

def weekly_report_for_subject(
    subject: str,
    configured_path: str,
    start: dt.datetime,
    end: dt.datetime,
    config: dict[str, Any],
    use_ai: bool,
    report_path: Path,
    answer_path: Path,
    write: bool = True,
    dirty_paths: set[str] | None = None,
) -> tuple[Path, Path]:
    commits = read_commits()
    weekly_commits = commits_for_week(commits, start, end)
    changed = collect_changed_paths(weekly_commits)
    bundle = build_subject_bundle(
        subject,
        changed,
        configured_path,
        dirty_paths,
        commits=weekly_commits,
    )

    metadata = [
        f"> 生成时间：{end.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        f"> 统计范围：{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')}（北京时间）",
        "> 数据口径：只读取该范围内 message 严格符合 `daily: YYYY-MM-DD` 的已提交内容。",
        f"> 本科目匹配到 {len([c for c in weekly_commits if c.daily_date is not None])} 个 daily 提交。",
    ]
    commits_text = format_commit_list(weekly_commits)
    problems = list(bundle.problems)

    if not bundle.sources:
        question_content = "\n".join(
            [
                f"# 周测｜{subject}",
                "",
                *metadata,
                "",
                "## 状态",
                "",
                "本周没有匹配到该科目的 daily 提交，因此未生成测试题。",
                "",
                "## Git 提交清单",
                "",
                commits_text,
                "",
            ]
        )
        answer_content = "\n".join(
            [
                f"# 周测答案与核验｜{subject}",
                "",
                *metadata,
                "",
                "本周没有测试题。",
                "",
            ]
        )
        write_or_preview_report(report_path, question_content, write)
        write_or_preview_report(answer_path, answer_content, write)
        return report_path, answer_path

    if use_ai:
        try:
            generated = call_openai(weekly_prompt(bundle, start, end, config), bundle, config)
            summary, test, answer = split_weekly_output(generated)
            summary = link_source_ids(summary, bundle, report_path)
            test = link_source_ids(test, bundle, report_path)
            answer = link_source_ids(answer, bundle, answer_path)
        except WorkflowError as exc:
            summary = f"## 过去一周总结\n\n> AI 生成失败：{exc}"
            test = "## 测试题\n\n> 因 AI 生成失败，本周未生成测试题。"
            answer = f"## 答案与核验\n\n> 无可核验内容。原始错误：{exc}"
    else:
        summary = "## 过去一周总结\n\n> 本次使用 `--no-ai`，未调用外部 AI。"
        test = "## 测试题\n\n> 本次使用 `--no-ai`，未生成测试题。"
        answer = "## 答案与核验\n\n> 本次使用 `--no-ai`，未生成答案。"

    used_ids = source_ids_in("\n".join([summary, test, answer]))
    valid_ids = {source.source_id for source in bundle.sources}
    unknown_ids = sorted(used_ids - valid_ids)
    if unknown_ids:
        problems.append(f"AI 输出了不存在的来源编号：{', '.join(unknown_ids)}")
    if not used_ids and use_ai:
        problems.append("AI 输出没有包含来源编号，题型/方法和试题无法完成来源核验。")

    common = [
        *metadata,
        "",
        "## Git 提交清单",
        "",
        commits_text,
        "",
    ]
    if problems:
        common.extend(["## 数据检查", "", *[f"- {problem}" for problem in problems], ""])
    question_content = "\n".join(
        [f"# 周测｜{subject}", "", *common, summary, "", test, "", "## 来源索引", "", source_index_markdown(bundle, report_path), ""]
    )
    answer_content = "\n".join(
        [
            f"# 周测答案与核验｜{subject}",
            "",
            *metadata,
            "",
            answer,
            "",
            "## 来源索引",
            "",
            source_index_markdown(bundle, answer_path),
            "",
        ]
    )
    write_or_preview_report(report_path, question_content, write)
    write_or_preview_report(answer_path, answer_content, write)
    return report_path, answer_path

def weekly_reports(
    end: dt.datetime,
    config: dict[str, Any],
    use_ai: bool = True,
    write: bool = True,
) -> list[Path]:
    window_days = int(config.get("weekly", {}).get("window_days", 7))
    start = end - dt.timedelta(days=window_days)
    week_date = (end.date() - dt.timedelta(days=1)).isocalendar()
    week_id = f"{week_date.year}-W{week_date.week:02d}"
    report_dir = ROOT / config["reports"]["weekly"]
    dirty_paths = read_uncommitted_paths()
    outputs: list[Path] = []
    for subject, configured_path in config.get("subjects", {}).items():
        report_path = report_dir / f"周测-{week_id}-{subject}.md"
        answer_path = report_dir / f"周测-{week_id}-{subject}-答案.md"
        weekly_report_for_subject(
            subject,
            configured_path,
            start,
            end,
            config,
            use_ai,
            report_path,
            answer_path,
            write=write,
            dirty_paths=dirty_paths,
        )
        outputs.extend([report_path, answer_path])
    return outputs
