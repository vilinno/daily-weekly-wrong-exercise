"""复盘追踪、掌握度测试与答案工作流。"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

from .foundation import ROOT, ReviewEntry, WorkflowError
from .git_store import read_uncommitted_paths
from .source_scanner import tracked_subject_bundle
from .review_state import build_review_statuses, load_review_log, review_status_table, select_review_sources
from .markdown_tools import source_index_markdown
from .prompts import review_prompt
from .quality import removable_incomplete_choice_numbers, remove_numbered_markdown_items, review_output_quality_issues, review_repair_prompt, review_verification_prompt
from .ai_client import call_openai
from .ai_output import link_source_ids, split_review_output
from .scheduling import current_run_time
from .report_io import write_report_pair
from .run_metadata import run_metadata_block
from .pipeline_validation import validate_generated_output

def review_report_for_subject(
    subject: str,
    configured_path: str,
    target_date: dt.date,
    config: dict[str, Any],
    entries: list[ReviewEntry],
    log_problems: list[str],
    use_ai: bool,
    write: bool,
    dirty_paths: set[str],
) -> tuple[Path, Path]:
    tracked_ref = str(config.get("git", {}).get("tracked_ref", "HEAD"))
    bundle = tracked_subject_bundle(subject, configured_path, dirty_paths, tracked_ref)
    statuses = build_review_statuses(bundle, entries, target_date, config)
    question_limit = int(config["review"].get("questions_per_subject", 8))
    selected = select_review_sources(
        bundle, statuses, target_date, question_limit
    )
    report_dir = ROOT / config["reports"]["review"]
    report_path = report_dir / f"复盘-{target_date.isoformat()}-{subject}.md"
    answer_path = report_dir / f"复盘-{target_date.isoformat()}-{subject}-答案.md"
    due_count = sum(status.due_on <= target_date for status in statuses)
    mastery_counts = {
        mastery: sum(status.mastery == mastery for status in statuses)
        for mastery in ("未阅读", "未测试", "薄弱", "部分掌握", "已掌握")
    }
    problems = [*log_problems, *bundle.problems]
    run_id = uuid.uuid4().hex
    question_ids = [source.question_id for source in selected.sources]
    prompts: list[str] = []
    generation_failed = False

    if selected.sources and use_ai:
        try:
            base_prompt = review_prompt(selected, statuses, target_date, config)
            prompts.append(base_prompt)
            generated = call_openai(base_prompt, selected, config)
            test, answer = split_review_output(generated)
            valid_ids = {source.source_id for source in selected.sources}
            quality_issues = review_output_quality_issues(
                test, answer, valid_ids, question_limit
            )
            if quality_issues:
                repair_prompt = review_repair_prompt(base_prompt, generated, quality_issues)
                prompts.append(repair_prompt)
                generated = call_openai(
                    repair_prompt,
                    selected,
                    config,
                )
                test, answer = split_review_output(generated)
                quality_issues = review_output_quality_issues(
                    test, answer, valid_ids, question_limit
                )
            if quality_issues:
                raise WorkflowError(
                    "复盘题未通过自动质量检查："
                    + "；".join(quality_issues)
                )
            if bool(config.get("ai", {}).get("verify_reviews", True)):
                verification_prompt = review_verification_prompt(
                    selected, generated, question_limit
                )
                prompts.append(verification_prompt)
                generated = call_openai(
                    verification_prompt,
                    selected,
                    config,
                )
                test, answer = split_review_output(generated)
                quality_issues = review_output_quality_issues(
                    test, answer, valid_ids, question_limit
                )
                if quality_issues:
                    removable = removable_incomplete_choice_numbers(quality_issues)
                    if removable:
                        test = remove_numbered_markdown_items(test, removable)
                        answer = remove_numbered_markdown_items(answer, removable)
                        quality_issues = review_output_quality_issues(
                            test, answer, valid_ids, question_limit
                        )
                    if quality_issues:
                        raise WorkflowError(
                            "复盘题二次核验结果未通过自动质量检查："
                            + "；".join(quality_issues)
                        )
            test = link_source_ids(test, selected, report_path)
            answer = link_source_ids(answer, selected, answer_path)
        except WorkflowError as exc:
            generation_failed = True
            problems.append(f"AI 生成或核验失败：{exc}")
            test = f"## 掌握度测试\n\n> AI 生成失败：{exc}"
            answer = f"## 答案与核验\n\n> 无可核验内容。原始错误：{exc}"
    elif selected.sources:
        problems.append("使用 --no-ai，未生成可验证掌握度测试和答案。")
        test = "## 掌握度测试\n\n> 本次使用 `--no-ai`，未生成测试题。"
        answer = "## 答案与核验\n\n> 本次使用 `--no-ai`，未生成答案。"
    else:
        problems.append("当前没有可用于命题的已提交来源。")
        test = "## 掌握度测试\n\n> 当前没有可用于命题的已提交来源。"
        answer = "## 答案与核验\n\n> 当前没有测试题。"

    common_metadata = [
        f"> 生成时间：{current_run_time().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        f"> 复盘日期：{target_date.isoformat()}",
        "> 数据口径：读取 HEAD 中已提交的题图、笔记和复盘记录；未提交科目资料不送往外部 AI。",
        "",
    ]
    status = validate_generated_output(
        generated=bool(prompts) and not generation_failed,
        issues=problems,
        hard_failure=generation_failed,
    ).status
    metadata_block = run_metadata_block(
        kind="review", status=status, config=config,
        question_ids=question_ids, prompts=prompts, issues=problems, run_id=run_id,
    )
    question_lines = [
        f"# 错题复盘｜{subject}｜{target_date.isoformat()}",
        "",
        *common_metadata,
        metadata_block,
        "",
        "## 复盘概览",
        "",
        f"- 共追踪 {len(statuses)} 个题目来源，其中 {due_count} 个已到期或从未复盘。",
        (
            "- 掌握状态："
            + "；".join(f"{key} {value}" for key, value in mastery_counts.items())
            + "。"
        ),
        f"- 本次选取 {len(selected.sources)} 个优先来源，目标生成约 {question_limit} 道掌握度测试题。",
        "",
    ]
    if problems:
        question_lines.extend(
            ["## 数据检查", "", *[f"- {problem}" for problem in problems], ""]
        )
    question_lines.extend(
        [
            "## 阅读、复盘与掌握状态",
            "",
            review_status_table(statuses, target_date)
            if statuses
            else "当前没有可追踪来源。",
            "",
            test,
            "",
            "## 完成后如何记录",
            "",
            (
                f"核对答案后，请在 `{config['review']['log_path']}` 的“记录”表中追加："
                "日期、题图或笔记路径、动作 `测试`、结果、正确率和遗忘点。"
            ),
            "记录提交到 Git 后，下一次复盘会自动调整掌握状态和到期时间。",
            "",
            "## 本次命题来源索引",
            "",
            source_index_markdown(selected, report_path)
            if selected.sources
            else "无。",
            "",
        ]
    )
    answer_lines = [
        f"# 错题复盘答案与核验｜{subject}｜{target_date.isoformat()}",
        "",
        *common_metadata,
        metadata_block,
        "",
        answer,
        "",
        "## 判定建议",
        "",
        "- `已掌握`：答案正确，且能说明关键条件、方法选择理由和易错点。",
        "- `部分掌握`：主要方法正确，但遗漏条件、边界情形或出现可纠正的小错误。",
        "- `薄弱`：方法选择错误、核心结论错误，或无法独立完成。",
        "",
        "## 来源索引",
        "",
        source_index_markdown(selected, answer_path)
        if selected.sources
        else "无。",
        "",
    ]
    write_report_pair(
        report_path, "\n".join(question_lines).rstrip() + "\n",
        answer_path, "\n".join(answer_lines).rstrip() + "\n", write,
    )
    return report_path, answer_path

def review_reports(
    target_date: dt.date,
    config: dict[str, Any],
    use_ai: bool = True,
    write: bool = True,
    subject_filter: str | None = None,
) -> list[Path]:
    dirty_paths = read_uncommitted_paths()
    tracked_ref = str(config.get("git", {}).get("tracked_ref", "HEAD"))
    entries, log_problems = load_review_log(config, dirty_paths, tracked_ref)
    future_entries = [entry for entry in entries if entry.reviewed_on > target_date]
    if future_entries:
        log_problems.append(
            f"复盘记录中有 {len(future_entries)} 条日期晚于本次复盘日期，已暂时忽略。"
        )
        entries = [entry for entry in entries if entry.reviewed_on <= target_date]
    outputs: list[Path] = []
    subjects = config.get("subjects", {})
    if subject_filter and subject_filter not in subjects:
        raise WorkflowError(
            f"未知科目 `{subject_filter}`；可选科目：{', '.join(subjects)}"
        )
    for subject, configured_path in subjects.items():
        if subject_filter and subject != subject_filter:
            continue
        outputs.extend(
            review_report_for_subject(
                subject,
                configured_path,
                target_date,
                config,
                entries,
                log_problems,
                use_ai,
                write,
                dirty_paths,
            )
        )
    return outputs
