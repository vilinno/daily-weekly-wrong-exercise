"""复盘记录解析、掌握度计算与来源选择。"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Iterable
from urllib.parse import unquote

from .foundation import HEADING_RE, ReviewEntry, ReviewStatus, Source, SubjectBundle, WorkflowError
from .git_store import normalize_repo_path, read_git_file, relative_repo_path
from .markdown_tools import obsidian_link, obsidian_target

def normalize_review_target(value: str) -> str:
    target = value.strip().strip("`")
    obsidian = re.fullmatch(r"\[\[([^\]]+)\]\]", target)
    if obsidian:
        target = obsidian.group(1).split("|", 1)[0]
    return normalize_repo_path(unquote(target.strip()))

def split_markdown_table_row(line: str) -> list[str]:
    value = line.strip()
    if not value.startswith("|") or not value.endswith("|"):
        return []
    return [
        cell.strip().replace("\\|", "|")
        for cell in re.split(r"(?<!\\)\|", value[1:-1])
    ]

def parse_review_log(content: str) -> tuple[list[ReviewEntry], list[str]]:
    """解析复盘记录表；坏行会报告但不会阻断其他记录。"""
    entries: list[ReviewEntry] = []
    problems: list[str] = []
    allowed_actions = {"阅读", "复盘", "测试"}
    allowed_results = {"完成", "未完成", "正确", "部分掌握", "错误", "已掌握"}
    in_records = False
    for line_number, line in enumerate(content.splitlines(), start=1):
        heading = HEADING_RE.match(line)
        if heading:
            in_records = heading.group(2).strip() == "记录"
            continue
        if not in_records:
            continue
        cells = split_markdown_table_row(line)
        if not cells or cells[0] in {"日期", "---"} or set(cells[0]) <= {"-", ":"}:
            continue
        if len(cells) != 6:
            problems.append(f"复盘记录第 {line_number} 行应有 6 列，当前为 {len(cells)} 列。")
            continue
        raw_date, raw_target, action, result, raw_score, note = cells
        try:
            reviewed_on = dt.date.fromisoformat(raw_date)
        except ValueError:
            problems.append(f"复盘记录第 {line_number} 行日期无效：`{raw_date}`")
            continue
        target = normalize_review_target(raw_target)
        if not target:
            problems.append(f"复盘记录第 {line_number} 行缺少来源路径。")
            continue
        if action not in allowed_actions:
            problems.append(f"复盘记录第 {line_number} 行动作无效：`{action}`")
            continue
        if result not in allowed_results:
            problems.append(f"复盘记录第 {line_number} 行结果无效：`{result}`")
            continue
        score: float | None = None
        if raw_score:
            score_text = raw_score.rstrip("%").strip()
            try:
                score = float(score_text)
            except ValueError:
                problems.append(f"复盘记录第 {line_number} 行正确率无效：`{raw_score}`")
                continue
            if not 0 <= score <= 100:
                problems.append(f"复盘记录第 {line_number} 行正确率应在 0 至 100 之间。")
                continue
        entries.append(ReviewEntry(reviewed_on, target, action, result, score, note))
    entries.sort(key=lambda entry: entry.reviewed_on)
    return entries, problems

def load_review_log(
    config: dict[str, Any], dirty_paths: set[str], tracked_ref: str = "HEAD"
) -> tuple[list[ReviewEntry], list[str]]:
    relative_path = normalize_repo_path(str(config["review"]["log_path"]))
    problems: list[str] = []
    if relative_path in dirty_paths:
        problems.append(
            f"复盘记录 `{relative_path}` 存在未提交修改；本次只读取 HEAD 中已提交的版本。"
        )
    try:
        content = read_git_file(tracked_ref, relative_path)
    except WorkflowError:
        problems.append(
            f"HEAD 中未找到复盘记录 `{relative_path}`；本次按没有历史记录处理。"
        )
        return [], problems
    entries, parse_problems = parse_review_log(content)
    return entries, problems + parse_problems

def review_targets_for_source(source: Source) -> set[str]:
    targets: set[str] = set()
    if source.question_id:
        targets.add(source.question_id)
    if source.image_path:
        targets.add(relative_repo_path(source.image_path))
    if source.note_path:
        note = relative_repo_path(source.note_path)
        targets.add(note)
        if source.title:
            targets.add(f"{note}#{source.title}")
    return {normalize_review_target(target) for target in targets}

def entries_for_source(
    source: Source, entries: Iterable[ReviewEntry]
) -> list[ReviewEntry]:
    targets = review_targets_for_source(source)
    return sorted(
        [entry for entry in entries if entry.target in targets],
        key=lambda entry: entry.reviewed_on,
    )

def latest_action_date(entries: Iterable[ReviewEntry], action: str) -> dt.date | None:
    dates = [
        entry.reviewed_on
        for entry in entries
        if entry.action == action
        and not (action in {"阅读", "复盘"} and entry.result == "未完成")
    ]
    return max(dates) if dates else None

def mastery_from_entries(entries: list[ReviewEntry]) -> str:
    tests = [entry for entry in entries if entry.action == "测试"]
    if tests:
        latest = tests[-1]
        if latest.score is not None:
            if latest.score >= 85:
                return "已掌握"
            if latest.score >= 60:
                return "部分掌握"
            return "薄弱"
        if latest.result in {"正确", "已掌握"}:
            return "已掌握"
        if latest.result == "部分掌握":
            return "部分掌握"
        if latest.result in {"错误", "未完成"}:
            return "薄弱"
    if any(
        entry.action in {"阅读", "复盘"} and entry.result != "未完成"
        for entry in entries
    ):
        return "未测试"
    return "未阅读"

def build_review_statuses(
    bundle: SubjectBundle,
    entries: Iterable[ReviewEntry],
    target_date: dt.date,
    config: dict[str, Any],
) -> list[ReviewStatus]:
    intervals = {
        key: int(value)
        for key, value in config["review"]["intervals_days"].items()
    }
    statuses: list[ReviewStatus] = []
    all_entries = list(entries)
    for source in bundle.sources:
        related = entries_for_source(source, all_entries)
        mastery = mastery_from_entries(related)
        last_read = latest_action_date(related, "阅读")
        last_review = latest_action_date(related, "复盘")
        last_test = latest_action_date(related, "测试")
        activity_dates = [
            value for value in (last_read, last_review, last_test) if value is not None
        ]
        if activity_dates:
            due_on = max(activity_dates) + dt.timedelta(days=intervals[mastery])
        else:
            due_on = target_date
        statuses.append(
            ReviewStatus(
                source=source,
                entries=related,
                last_read=last_read,
                last_review=last_review,
                last_test=last_test,
                mastery=mastery,
                due_on=due_on,
            )
        )
    return statuses

def review_priority(status: ReviewStatus, target_date: dt.date) -> tuple[int, int, str]:
    mastery_order = {
        "薄弱": 0,
        "未阅读": 1,
        "未测试": 2,
        "部分掌握": 3,
        "已掌握": 4,
    }
    overdue_days = (target_date - status.due_on).days
    return (
        0 if status.due_on <= target_date else 1,
        mastery_order.get(status.mastery, 9) * 10000 - overdue_days,
        status.source.title,
    )

def select_review_sources(
    bundle: SubjectBundle,
    statuses: list[ReviewStatus],
    target_date: dt.date,
    limit: int,
) -> SubjectBundle:
    selected_statuses = sorted(
        statuses, key=lambda status: review_priority(status, target_date)
    )[:limit]
    selected = [status.source for status in selected_statuses]
    selected_notes = {
        source.note_path for source in selected if source.note_path is not None
    }
    return SubjectBundle(
        subject=bundle.subject,
        changed_paths=bundle.changed_paths,
        sources=selected,
        problems=list(bundle.problems),
        note_texts={
            path: content
            for path, content in bundle.note_texts.items()
            if path in selected_notes
        },
    )

def format_review_date(value: dt.date | None) -> str:
    return value.isoformat() if value else "未记录"

def review_status_table(
    statuses: list[ReviewStatus], target_date: dt.date
) -> str:
    rows = [
        "| 来源 | 知识点/题型 | 最近阅读 | 最近复盘 | 最近测试 | 掌握状态 | 下次复盘 |",
        "|---|---|---|---|---|---|---|",
    ]
    for status in sorted(statuses, key=lambda item: review_priority(item, target_date)):
        source = status.source
        target = source.image_path or source.note_path
        source_cell = (
            obsidian_link(source.source_id, obsidian_target(target))
            if target
            else source.source_id
        )
        due = status.due_on.isoformat()
        if status.due_on <= target_date:
            due += "（到期）"
        rows.append(
            "| "
            + " | ".join(
                [
                    source_cell,
                    source.title.replace("|", "\\|"),
                    format_review_date(status.last_read),
                    format_review_date(status.last_review),
                    format_review_date(status.last_test),
                    status.mastery,
                    due,
                ]
            )
            + " |"
        )
    return "\n".join(rows)

def review_history_payload(statuses: Iterable[ReviewStatus]) -> str:
    rows: list[str] = []
    for status in statuses:
        source = status.source
        rows.append(
            f"- {source.source_id}：掌握状态={status.mastery}；"
            f"最近阅读={format_review_date(status.last_read)}；"
            f"最近复盘={format_review_date(status.last_review)}；"
            f"最近测试={format_review_date(status.last_test)}；"
            f"下次复盘={status.due_on.isoformat()}"
        )
        notes = [entry.note for entry in status.entries if entry.note]
        if notes:
            rows.append(f"  - 历史备注：{'；'.join(notes[-3:])}")
    return "\n".join(rows)
