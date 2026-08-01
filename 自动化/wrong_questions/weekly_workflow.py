"""每周测试与答案工作流。"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

from .foundation import ROOT, WorkflowError
from .git_store import collect_changed_paths, commits_for_week, read_commits, read_uncommitted_paths
from .source_scanner import build_subject_bundle
from .markdown_tools import format_commit_list, source_index_markdown
from .prompts import weekly_prompt
from .ai_client import call_openai
from .ai_output import link_source_ids, source_ids_in, split_weekly_output

from .report_io import write_or_preview_report, write_report_pair
from .run_metadata import run_metadata_block
from .pipeline_validation import validate_generated_output, validate_source_ids

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
    tracked_ref = str(config.get("git", {}).get("tracked_ref", "HEAD"))
    commits = read_commits(tracked_ref)
    weekly_commits = commits_for_week(commits, start, end)
    changed = collect_changed_paths(weekly_commits)
    bundle = build_subject_bundle(
        subject,
        changed,
        configured_path,
        dirty_paths,
        commits=weekly_commits,
    )
    run_id = uuid.uuid4().hex
    question_ids = [source.question_id for source in bundle.sources]
    prompts: list[str] = []
    generation_failed = False

    metadata = [
        f"> 生成时间：{end.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        f"> tracked_ref：`{tracked_ref}`",
        f"> 统计范围：{start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')}（北京时间）",
        "> 数据口径：只读取该范围内 message 严格符合 `daily: YYYY-MM-DD` 的已提交内容。",
        f"> 本科目匹配到 {len([c for c in weekly_commits if c.daily_date is not None])} 个 daily 提交。",
    ]
    commits_text = format_commit_list(weekly_commits)
    problems = list(bundle.problems)

    if not bundle.sources:
        metadata_block = run_metadata_block(
            kind="weekly", status="needs_review", config=config,
            question_ids=question_ids,
            issues=[*bundle.problems, "本周没有可用来源。"], run_id=run_id,
        )
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
                metadata_block,
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
                metadata_block,
                "",
            ]
        )
        write_report_pair(report_path, question_content, answer_path, answer_content, write)
        return report_path, answer_path

    if use_ai:
        try:
            prompt = weekly_prompt(bundle, start, end, config)
            prompts.append(prompt)
            generated = call_openai(prompt, bundle, config)
            summary, test, answer = split_weekly_output(generated)
            summary = link_source_ids(summary, bundle, report_path)
            test = link_source_ids(test, bundle, report_path)
            answer = link_source_ids(answer, bundle, answer_path)
        except WorkflowError as exc:
            generation_failed = True
            problems.append(f"AI 生成失败：{exc}")
            summary = f"## 过去一周总结\n\n> AI 生成失败：{exc}"
            test = "## 测试题\n\n> 因 AI 生成失败，本周未生成测试题。"
            answer = f"## 答案与核验\n\n> 无可核验内容。原始错误：{exc}"
    else:
        problems.append("使用 --no-ai，未生成可验证周测题和答案。")
        summary = "## 过去一周总结\n\n> 本次使用 `--no-ai`，未调用外部 AI。"
        test = "## 测试题\n\n> 本次使用 `--no-ai`，未生成测试题。"
        answer = "## 答案与核验\n\n> 本次使用 `--no-ai`，未生成答案。"

    valid_ids = {source.source_id for source in bundle.sources}
    generated_texts = [summary, test, answer]
    used_ids = set().union(*(set(source_ids_in(text)) for text in generated_texts))
    unknown_ids = validate_source_ids(generated_texts, valid_ids)
    if unknown_ids:
        generation_failed = True
        problems.append(f"AI 输出了不存在的来源编号：{', '.join(unknown_ids)}")
    if not used_ids and use_ai:
        generation_failed = True
        problems.append("AI 输出没有包含来源编号，题型/方法和试题无法完成来源核验。")

    status = validate_generated_output(
        generated=bool(prompts) and not generation_failed,
        issues=problems,
        hard_failure=generation_failed,
    ).status
    metadata_block = run_metadata_block(
        kind="weekly", status=status, config=config,
        question_ids=question_ids, prompts=prompts, issues=problems, run_id=run_id,
    )

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
        [f"# 周测｜{subject}", "", *common, metadata_block, "", summary, "", test, "", "## 来源索引", "", source_index_markdown(bundle, report_path), ""]
    )
    answer_content = "\n".join(
        [
            f"# 周测答案与核验｜{subject}",
            "",
            *metadata,
            "",
            metadata_block,
            "",
            answer,
            "",
            "## 来源索引",
            "",
            source_index_markdown(bundle, answer_path),
            "",
        ]
    )
    write_report_pair(report_path, question_content, answer_path, answer_content, write)
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
