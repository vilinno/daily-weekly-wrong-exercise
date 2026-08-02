"""每周测试与答案工作流。"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

from .foundation import ROOT, WorkflowError
from .git_store import collect_changed_paths, commits_for_week, parent_commit_sha, read_commits, read_uncommitted_paths, relative_repo_path
from .source_scanner import build_subject_bundle
from .markdown_tools import format_commit_list, source_index_markdown
from .prompts import weekly_prompt
from .ai_client import call_openai
from .ai_output import link_source_ids, source_ids_in, split_weekly_output

from .report_io import workflow_entry, write_or_preview_report, write_report_pair
from .run_metadata import run_metadata_block
from .pipeline_validation import validate_generated_output, validate_source_ids
from .quality import answer_leakage_issues, review_output_quality_issues

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
    tracked_ref = str(config.get("git", {}).get("tracked_ref", "refs/heads/main"))
    commits = read_commits(tracked_ref)
    weekly_commits = commits_for_week(commits, start, end)
    source_commits = [commit.sha for commit in weekly_commits]
    base_commit = parent_commit_sha(weekly_commits[0].sha) if weekly_commits else None
    tip_commit = weekly_commits[-1].sha if weekly_commits else None
    changed = collect_changed_paths(weekly_commits)
    bundle = build_subject_bundle(
        subject,
        changed,
        configured_path,
        dirty_paths,
        commits=weekly_commits,
        revision=tip_commit,
    )
    run_id = uuid.uuid4().hex
    question_ids = [source.question_id for source in bundle.sources]
    prompts: list[str] = []
    ai_calls: list[dict[str, str | None]] = []
    generation_failed = False
    raw_output = ""

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
            base_commit=base_commit, tip_commit=tip_commit,
            source_commits=source_commits, scope_kind="range",
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
            result = call_openai(prompt, bundle, config, role="generation")
            ai_calls.append(result.metadata())
            generated = result.text
            summary, test, answer = split_weekly_output(generated)
            raw_output = generated
            summary = link_source_ids(summary, bundle, report_path)
            test = link_source_ids(test, bundle, report_path)
            answer = link_source_ids(answer, bundle, answer_path)
        except WorkflowError as exc:
            generation_failed = True
            problems.append(f"AI 生成失败：{exc}")
            summary = f"## 过去一周总结\n\n> AI 生成失败：{exc}"
            test = "## 测试题\n\n> 因 AI 生成失败，本周未生成测试题。"
            answer = f"## 答案与核验\n\n> 无可核验内容。原始错误：{exc}"
            raw_output = "\n\n".join([summary, test, answer])
    else:
        problems.append("使用 --no-ai，未生成可验证周测题和答案。")
        summary = "## 过去一周总结\n\n> 本次使用 `--no-ai`，未调用外部 AI。"
        test = "## 测试题\n\n> 本次使用 `--no-ai`，未生成测试题。"
        answer = "## 答案与核验\n\n> 本次使用 `--no-ai`，未生成答案。"
        raw_output = "\n\n".join([summary, test, answer])

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

    quality_issues: list[str] = []
    leakage_issues: list[str] = []
    if use_ai and not generation_failed:
        expected_questions = int(config.get("weekly", {}).get("questions_per_subject", 10))
        quality_issues = review_output_quality_issues(
            test, answer, valid_ids, expected_questions
        )
        leakage_issues = answer_leakage_issues(test, answer)
        problems.extend(quality_issues)
        problems.extend(leakage_issues)

    validation = validate_generated_output(
        generated=bool(prompts) and not generation_failed,
        issues=problems,
        hard_failure=generation_failed or bool(quality_issues) or bool(leakage_issues),
        structure_verified=bool(prompts) and not generation_failed and not quality_issues,
        sources_verified=bool(prompts) and not generation_failed and not unknown_ids and bool(used_ids),
        domain_verified=False,
        requires_answer_pair=True,
        answer_pair_verified=bool(prompts) and not generation_failed and not quality_issues,
        answer_leakage_free=bool(prompts) and not generation_failed and not leakage_issues,
    )
    status = validation.status
    metadata_issues = [
        *problems,
        *[
            item["message"]
            for item in validation.issues
            if item["message"] not in problems
        ],
    ]
    metadata_block = run_metadata_block(
        kind="weekly", status=status, config=config,
        question_ids=question_ids, prompts=prompts, issues=metadata_issues, run_id=run_id,
        ai_calls=ai_calls,
        base_commit=base_commit, tip_commit=tip_commit,
        source_commits=source_commits, scope_kind="range",
    )

    raw_output_path: Path | None = None
    if status == "rejected":
        raw_output_path = ROOT / ".runs" / run_id / "raw-output.md"
        write_or_preview_report(
            raw_output_path,
            "# 被拒绝的周测原始输出\n\n"
            "> 此文件仅供人工诊断，不能作为正式测试或答案使用。\n\n"
            + raw_output.rstrip()
            + "\n",
            write,
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
    if status == "rejected":
        rejected_notice = [
            "## 状态",
            "",
            "> 本次周测已被自动门禁拒绝，正式报告不展示被拒绝的题目或答案。",
            f"> 原始输出仅保存于 `{relative_repo_path(raw_output_path)}`，请修复问题后重新生成。",
            "",
        ]
        question_tail = rejected_notice
        answer_tail = rejected_notice
    else:
        question_tail = [summary, "", test, ""]
        answer_tail = [answer, ""]
    question_content = "\n".join(
        [f"# 周测｜{subject}", "", *common, metadata_block, "", *question_tail, "## 来源索引", "", source_index_markdown(bundle, report_path), ""]
    )
    answer_content = "\n".join(
        [f"# 周测答案与核验｜{subject}", "", *metadata, "", metadata_block, "", *answer_tail, "## 来源索引", "", source_index_markdown(bundle, answer_path), ""]
    )
    write_report_pair(report_path, question_content, answer_path, answer_content, write)
    return report_path, answer_path

@workflow_entry
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
