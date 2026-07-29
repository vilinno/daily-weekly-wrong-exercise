"""每日错题统计工作流。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from .foundation import ROOT, WorkflowError
from .git_store import collect_changed_paths, commit_local_date_issues, commits_for_daily, read_commits, read_uncommitted_paths, subject_for_path
from .source_scanner import build_subject_bundle
from .markdown_tools import format_changed_files, format_commit_list, source_index_markdown
from .prompts import daily_prompt
from .ai_client import call_openai
from .ai_output import link_source_ids, source_ids_in
from .scheduling import all_commits_in_local_day, current_run_time
from .report_io import write_or_preview_report

def daily_report(
    target_date: dt.date,
    config: dict[str, Any],
    use_ai: bool = True,
    write: bool = True,
) -> Path:
    commits = read_commits()
    daily_commits = commits_for_daily(commits, target_date)
    day_commits = all_commits_in_local_day(commits, target_date)
    changed = collect_changed_paths(daily_commits)
    subjects = config.get("subjects", {})
    dirty_paths = read_uncommitted_paths()
    report_dir = ROOT / config["reports"]["daily"]
    report_path = report_dir / f"日报-{target_date.isoformat()}.md"

    lines = [
        f"# 每日错题统计｜{target_date.isoformat()}",
        "",
        f"> 生成时间：{current_run_time().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        "> 数据口径：只读取 Git 中 message 严格符合 `daily: YYYY-MM-DD` 的已提交内容；未提交内容不纳入统计。",
        "",
        "## Git 提交检查",
        "",
        f"本日匹配到 {len(daily_commits)} 个 daily 提交。",
        format_commit_list(daily_commits),
        "",
    ]
    message_issues = commit_local_date_issues(day_commits)
    if not daily_commits:
        message_issues.append(
            f"未找到 `daily: {target_date.isoformat()}`；请确认当天错题已在 22:30 前提交。"
        )
    if message_issues:
        lines.append("### 需要检查")
        lines.extend(f"- {issue}" for issue in message_issues)
        lines.append("")
    else:
        lines.append("- 提交 message 和北京时间日期均符合约定。")
        lines.append("")

    lines.extend(["## 变更文件", "", format_changed_files(changed), ""])
    lines.extend(
        [
            "## 未提交内容",
            "",
            "- 工作区中的未提交内容不会纳入本次统计。",
            "",
        ]
    )
    dirty_source_paths = sorted(
        path for path in dirty_paths if subject_for_path(path, subjects) is not None
    )
    if dirty_source_paths:
        lines.extend(
            [
                "以下科目文件当前存在未提交修改，相关来源已跳过：",
                *[f"- `{path}`" for path in dirty_source_paths],
                "",
            ]
        )
    else:
        lines.extend(["- 未发现数学或 408 目录下的未提交文件。", ""])
    lines.extend(["## 科目统计", ""])
    if not daily_commits:
        lines.append("本日没有可统计的 daily 提交，因此不调用 AI，不生成题目归纳。")
    else:
        for subject, configured_path in subjects.items():
            bundle = build_subject_bundle(
                subject,
                changed,
                configured_path,
                dirty_paths,
                commits=daily_commits,
            )
            lines.append(f"### {subject}")
            lines.append("")
            lines.append(
                f"- 变更文件：{len(bundle.changed_paths)} 个；增量来源：{len(bundle.sources)} 个。"
            )
            if bundle.problems:
                lines.append("- 数据检查：发现以下项目需要人工确认：")
                lines.extend(f"  - {problem}" for problem in bundle.problems)
            if not bundle.sources:
                lines.append("- 本日没有该科目的新增或修改笔记/题图。")
                lines.append("")
                continue
            if use_ai:
                try:
                    generated = call_openai(daily_prompt(bundle, target_date, config), bundle, config)
                    generated = link_source_ids(generated, bundle, report_path)
                    used_ids = source_ids_in(generated)
                    valid_ids = {source.source_id for source in bundle.sources}
                    unknown_ids = sorted(used_ids - valid_ids)
                    lines.extend(["", "#### AI 归纳", "", generated])
                    if unknown_ids:
                        lines.append("")
                        lines.append(f"> 警告：AI 输出了不存在的来源编号：{', '.join(unknown_ids)}。")
                except WorkflowError as exc:
                    lines.extend(["", "#### AI 归纳", "", f"> AI 生成失败：{exc}"])
            else:
                lines.extend(["", "#### AI 归纳", "", "> 本次使用 `--no-ai`，未调用外部 AI。"])
            lines.extend(["", "#### 来源索引", "", source_index_markdown(bundle, report_path), ""])

    content = "\n".join(lines).rstrip() + "\n"
    write_or_preview_report(report_path, content, write)
    return report_path
