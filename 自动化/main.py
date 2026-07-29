"""每日错题自动化工作流。

默认只读取已经提交到 Git 的内容，只生成报告文件，不修改笔记、题图或 Git 历史。
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import mimetypes
import os
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ normally has zoneinfo
    ZoneInfo = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_DIR = Path(__file__).resolve().parent
CONFIG_PATH = AUTOMATION_DIR / "config.json"
ENV_PATH = AUTOMATION_DIR / ".env"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DAILY_RE = re.compile(r"^daily: (?P<date>\d{4}-\d{2}-\d{2})$")
WEEKLY_RE = re.compile(r"^weekly: (?P<week>\d{4}-W\d{2})$")
ALLOWED_MESSAGE_RES = (
    DAILY_RE,
    WEEKLY_RE,
    re.compile(r"^(?:docs|chore|fix): .+$"),
)
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]*)\)")
OBSIDIAN_IMAGE_RE = re.compile(r"!\[\[([^\]\r\n]+)\]\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
SOURCE_ID_RE = re.compile(r"\bS\d{3}\b")
DIFF_HUNK_RE = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
WEEKDAY_NUMBERS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def get_timezone() -> dt.tzinfo:
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Asia/Shanghai")
        except Exception:
            pass
    return dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")


BEIJING = get_timezone()


class WorkflowError(RuntimeError):
    """可向用户展示的工作流错误。"""


@dataclass(frozen=True)
class Commit:
    sha: str
    committed_at: dt.datetime
    message: str

    @property
    def short_sha(self) -> str:
        return self.sha[:8]

    @property
    def daily_date(self) -> dt.date | None:
        match = DAILY_RE.fullmatch(self.message)
        if not match:
            return None
        try:
            return dt.date.fromisoformat(match.group("date"))
        except ValueError:
            return None


@dataclass
class Source:
    source_id: str
    subject: str
    note_path: Path | None = None
    image_path: Path | None = None
    raw_image_ref: str | None = None
    headings: list[str] = field(default_factory=list)
    context: str = ""
    change_kind: str = "题目"
    line_number: int | None = None

    @property
    def title(self) -> str:
        if self.headings:
            return " / ".join(self.headings)
        if self.image_path:
            return self.image_path.stem
        if self.note_path:
            return self.note_path.stem
        return "未命名题目"


@dataclass
class SubjectBundle:
    subject: str
    changed_paths: list[str]
    sources: list[Source]
    problems: list[str]
    note_texts: dict[Path, str]


@dataclass
class NoteDelta:
    """某个 Markdown 在本次 Git 范围内的新增/修改行。"""

    relative_path: str
    line_ranges: list[tuple[int, int]]
    content: str | None
    revision: str


@dataclass(frozen=True)
class ReviewEntry:
    """用户在复盘记录表中填写的一条阅读、复盘或测试记录。"""

    reviewed_on: dt.date
    target: str
    action: str
    result: str
    score: float | None = None
    note: str = ""


@dataclass
class ReviewStatus:
    """某个题目来源的复盘状态。"""

    source: Source
    entries: list[ReviewEntry]
    last_read: dt.date | None
    last_review: dt.date | None
    last_test: dt.date | None
    mastery: str
    due_on: dt.date


def load_dotenv(path: Path = ENV_PATH) -> None:
    """加载简单 KEY=VALUE 格式的本地配置，不覆盖已有环境变量。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise WorkflowError(f"缺少配置文件：{CONFIG_PATH}")
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"配置文件不是有效 JSON：{CONFIG_PATH}\n{exc}") from exc
    return validate_config(config)


def parse_clock_time(value: Any, label: str) -> dt.time:
    text = str(value).strip()
    match = re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text)
    if not match:
        raise WorkflowError(f"{label} 应为 24 小时制 HH:MM，例如 22:30；当前值：{value}")
    hour, minute = (int(part) for part in text.split(":"))
    return dt.time(hour=hour, minute=minute)


def parse_weekday(value: Any, label: str = "weekly.weekday") -> int:
    key = str(value).strip().lower()
    if key not in WEEKDAY_NUMBERS:
        allowed = ", ".join(name.title() for name in WEEKDAY_NUMBERS)
        raise WorkflowError(f"{label} 应为英文星期名称（{allowed}）；当前值：{value}")
    return WEEKDAY_NUMBERS[key]


def validate_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise WorkflowError("配置文件顶层必须是 JSON 对象。")
    if config.get("timezone") != "Asia/Shanghai":
        raise WorkflowError("配置中的 timezone 必须是 Asia/Shanghai（北京时间）。")

    daily = config.get("daily")
    weekly = config.get("weekly")
    subjects = config.get("subjects")
    reports = config.get("reports")
    review = config.get("review")
    if not isinstance(daily, dict) or not isinstance(weekly, dict):
        raise WorkflowError("配置必须同时包含 daily 和 weekly 配置。")
    if not isinstance(subjects, dict) or not {"数学", "408"}.issubset(subjects):
        raise WorkflowError("subjects 必须同时配置数学和 408，且两科要分开生成报告。")
    if not isinstance(reports, dict) or not reports.get("daily") or not reports.get("weekly"):
        raise WorkflowError("reports 必须配置 daily 和 weekly 输出目录。")
    if not isinstance(review, dict):
        raise WorkflowError("配置必须包含 review 复盘配置。")

    parse_clock_time(daily.get("time"), "daily.time")
    parse_clock_time(weekly.get("time"), "weekly.time")
    parse_weekday(weekly.get("weekday"))
    try:
        window_days = int(weekly.get("window_days", 7))
        questions = int(weekly.get("questions_per_subject", 10))
        variant_ratio = float(weekly.get("variant_ratio", 0.3))
    except (TypeError, ValueError) as exc:
        raise WorkflowError("weekly.window_days、questions_per_subject、variant_ratio 必须是数字。") from exc
    if window_days <= 0:
        raise WorkflowError("weekly.window_days 必须大于 0。")
    if questions <= 0:
        raise WorkflowError("weekly.questions_per_subject 必须大于 0。")
    if not 0 <= variant_ratio <= 1:
        raise WorkflowError("weekly.variant_ratio 必须在 0 到 1 之间。")

    try:
        review_questions = int(review.get("questions_per_subject", 8))
    except (TypeError, ValueError) as exc:
        raise WorkflowError("review.questions_per_subject 必须是整数。") from exc
    if review_questions <= 0:
        raise WorkflowError("review.questions_per_subject 必须大于 0。")
    intervals = review.get("intervals_days")
    required_intervals = {"未阅读", "未测试", "薄弱", "部分掌握", "已掌握"}
    if not isinstance(intervals, dict) or not required_intervals.issubset(intervals):
        raise WorkflowError(
            "review.intervals_days 必须包含未阅读、未测试、薄弱、部分掌握、已掌握。"
        )
    try:
        parsed_intervals = {key: int(intervals[key]) for key in required_intervals}
    except (TypeError, ValueError) as exc:
        raise WorkflowError("review.intervals_days 中的值必须是非负整数。") from exc
    if any(value < 0 for value in parsed_intervals.values()):
        raise WorkflowError("review.intervals_days 中的值不能小于 0。")

    report_keys = ("daily", "weekly", "review", "correction")
    for key in report_keys:
        if not reports.get(key):
            raise WorkflowError(f"reports 必须配置 {key} 输出目录。")
        report_path = Path(str(reports[key]))
        if report_path.is_absolute() or ".." in report_path.parts:
            raise WorkflowError(f"reports.{key} 必须是仓库内的相对目录。")
    raw_log_path = review.get("log_path")
    if not raw_log_path:
        raise WorkflowError("review.log_path 不能为空。")
    log_path = Path(str(raw_log_path))
    if log_path.is_absolute() or ".." in log_path.parts:
        raise WorkflowError("review.log_path 必须是仓库内的相对路径。")
    return config


def run_git(*args: str) -> str:
    command = ["git", "-C", str(ROOT), *args]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise WorkflowError(f"Git 命令执行失败：git {' '.join(args)}\n{detail}")
    return completed.stdout


def normalize_repo_path(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def read_uncommitted_paths() -> set[str]:
    """返回工作区中未提交的路径，供统计时排除。"""
    paths: set[str] = set()
    commands = (
        ("-c", "core.quotePath=false", "diff", "--name-only"),
        ("-c", "core.quotePath=false", "diff", "--cached", "--name-only"),
        ("-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard"),
    )
    for command in commands:
        output = run_git(*command)
        for line in output.splitlines():
            normalized = normalize_repo_path(line.strip())
            if normalized:
                paths.add(normalized)
    return paths


def parse_git_datetime(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowError(f"无法解析 Git 提交时间：{value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(BEIJING)


def read_commits() -> list[Commit]:
    output = run_git("log", "--all", "--format=%H%x09%cI%x09%s")
    commits: list[Commit] = []
    for line in output.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3:
            continue
        sha, committed_at, message = fields
        commits.append(Commit(sha, parse_git_datetime(committed_at), message))
    return commits


def parent_commit_sha(commit_sha: str) -> str:
    """返回提交的第一个父提交；根提交使用 Git 的空树对象。"""
    output = run_git("rev-list", "--parents", "-n", "1", commit_sha).strip()
    fields = output.split()
    if len(fields) >= 2:
        return fields[1]
    if fields and fields[0] == commit_sha:
        return "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    raise WorkflowError(f"无法确定提交 `{commit_sha}` 的父提交。")


def read_git_file(commit_sha: str, relative_path: str) -> str:
    """读取指定提交中的 UTF-8 文本文件。"""
    return run_git(
        "-c",
        "core.quotePath=false",
        "show",
        f"{commit_sha}:{normalize_repo_path(relative_path)}",
    )


def parse_diff_new_line_ranges(diff_text: str) -> list[tuple[int, int]]:
    """解析 unified diff 中对应新文件的行范围。"""
    ranges: list[tuple[int, int]] = []
    for line in diff_text.splitlines():
        match = DIFF_HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group("new_start"))
        count = int(match.group("new_count") or "1")
        if count > 0:
            ranges.append((start, start + count - 1))
    return merge_line_ranges(ranges)


def merge_line_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或相邻的行范围，避免同一变更被重复处理。"""
    ordered = sorted((start, end) for start, end in ranges if start <= end)
    if not ordered:
        return []
    merged: list[list[int]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        previous = merged[-1]
        if start <= previous[1] + 1:
            previous[1] = max(previous[1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def note_deltas_for_scope(
    commits: Iterable[Commit], changed_paths: dict[str, set[str]]
) -> dict[str, NoteDelta]:
    """以本次范围的首个父提交为基线，提取各 Markdown 的净新增内容。"""
    ordered_commits = sorted(commits, key=lambda commit: commit.committed_at)
    if not ordered_commits:
        return {}

    base_sha = parent_commit_sha(ordered_commits[0].sha)
    tip_sha = ordered_commits[-1].sha
    deltas: dict[str, NoteDelta] = {}
    for relative_path in sorted(changed_paths):
        if not relative_path.lower().endswith(".md"):
            continue
        diff_text = run_git(
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-ext-diff",
            "--unified=0",
            "--find-renames",
            "--find-copies",
            base_sha,
            tip_sha,
            "--",
            relative_path,
        )
        ranges = parse_diff_new_line_ranges(diff_text)
        try:
            content = read_git_file(tip_sha, relative_path)
        except WorkflowError:
            content = None
        deltas[relative_path] = NoteDelta(relative_path, ranges, content, tip_sha)
    return deltas


def is_allowed_message(message: str) -> bool:
    return any(pattern.fullmatch(message) for pattern in ALLOWED_MESSAGE_RES)


def commit_local_date_issues(commits: Iterable[Commit]) -> list[str]:
    issues: list[str] = []
    for commit in commits:
        if not is_allowed_message(commit.message):
            issues.append(
                f"提交 {commit.short_sha} 的 message 不符合约定：`{commit.message}`"
            )
        if commit.daily_date is not None and commit.daily_date != commit.committed_at.date():
            issues.append(
                f"提交 {commit.short_sha} 的日期不一致：message 为 "
                f"{commit.daily_date.isoformat()}，北京时间提交日期为 "
                f"{commit.committed_at.date().isoformat()}"
            )
    return issues


def commits_for_daily(commits: Iterable[Commit], target_date: dt.date) -> list[Commit]:
    return sorted(
        [commit for commit in commits if commit.daily_date == target_date],
        key=lambda commit: commit.committed_at,
    )


def commits_for_week(
    commits: Iterable[Commit], start: dt.datetime, end: dt.datetime
) -> list[Commit]:
    return sorted(
        [
            commit
            for commit in commits
            if commit.daily_date is not None
            and start <= commit.committed_at < end
        ],
        key=lambda commit: commit.committed_at,
    )


def changed_paths_for_commit(commit: Commit) -> list[tuple[str, str]]:
    output = run_git(
        "-c",
        "core.quotePath=false",
        "show",
        "--format=",
        "--name-status",
        "--find-renames",
        "--find-copies",
        commit.sha,
    )
    changes: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip() or "\t" not in line:
            continue
        fields = line.split("\t")
        status = fields[0]
        if status.startswith(("R", "C")) and len(fields) >= 3:
            changes.append((status, fields[-1]))
        else:
            changes.append((status, fields[-1]))
    return changes


def collect_changed_paths(commits: Iterable[Commit]) -> dict[str, set[str]]:
    changed: dict[str, set[str]] = {}
    for commit in commits:
        for status, path in changed_paths_for_commit(commit):
            normalized = normalize_repo_path(path)
            changed.setdefault(normalized, set()).add(status)
    return changed


def is_ignored_source_path(relative_path: str) -> bool:
    normalized = normalize_repo_path(relative_path)
    first = normalized.split("/", 1)[0]
    return first in {".git", "自动化", "报告", "过渡站"} or normalized in {
        "README.md",
        "AGENTS.md",
    }


def repo_path(relative_path: str) -> Path:
    return (ROOT / Path(relative_path.replace("/", os.sep))).resolve()


def relative_repo_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def subject_for_path(relative_path: str, subjects: dict[str, str]) -> str | None:
    normalized = normalize_repo_path(relative_path)
    first = normalized.split("/", 1)[0]
    for subject, configured_path in subjects.items():
        configured = configured_path.replace("\\", "/").strip("/")
        if first == configured.split("/", 1)[0]:
            return subject
    return None


def parse_image_target(inner: str) -> str:
    value = inner.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    if value.startswith(('"', "'")):
        quote = value[0]
        end = value.find(quote, 1)
        if end > 0:
            return value[1:end]
    return value.split()[0] if value.split() else ""


def parse_obsidian_image_target(inner: str) -> str:
    """提取 Obsidian 图片嵌入中的仓库内路径，忽略尺寸或别名。"""
    value = inner.strip()
    if "|" in value:
        value = value.split("|", 1)[0]
    return value.strip()


def iter_image_targets(line: str) -> Iterable[str]:
    """按原文顺序返回标准 Markdown 和 Obsidian 图片引用。"""
    matches: list[tuple[int, str]] = []
    matches.extend(
        (match.start(), parse_image_target(match.group(1)))
        for match in IMAGE_RE.finditer(line)
    )
    for match in OBSIDIAN_IMAGE_RE.finditer(line):
        raw_ref = parse_obsidian_image_target(match.group(1))
        # Obsidian 也能嵌入笔记、音频等非图片文件；这里只把图片嵌入作为题图来源。
        if Path(unquote(raw_ref)).suffix.lower() in IMAGE_SUFFIXES:
            matches.append((match.start(), raw_ref))
    for _, raw_ref in sorted(matches, key=lambda item: item[0]):
        if raw_ref:
            yield raw_ref


def resolve_image_reference(note_path: Path, raw_ref: str) -> Path | None:
    decoded = unquote(raw_ref.strip())
    if not decoded or decoded.startswith(("http://", "https://", "data:")):
        return None

    candidates: list[Path] = []
    raw_path = Path(decoded)
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.append(note_path.parent / raw_path)
        candidates.append(ROOT / raw_path)

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()

    # 兼容历史绝对路径失效、但题图文件名仍然唯一的情况。
    filename = decoded.replace("\\", "/").rsplit("/", 1)[-1]
    matches = [
        path
        for path in ROOT.rglob(filename)
        if path.is_file() and ".git" not in path.parts
    ]
    return matches[0].resolve() if len(matches) == 1 else None


def note_section_context(
    content: str, anchor_line_number: int, max_chars: int = 4000
) -> str:
    """提取题图所在的三级专题段落，包含后续总结/解答。"""
    lines = content.splitlines()
    anchor_index = max(0, min(anchor_line_number - 1, len(lines) - 1))
    section_start: int | None = None
    section_level: int | None = None
    for index in range(anchor_index, -1, -1):
        heading = HEADING_RE.match(lines[index])
        if heading and len(heading.group(1)) <= 3:
            section_start = index
            section_level = len(heading.group(1))
            break
    if section_start is None or section_level is None:
        start = max(0, anchor_index - 3)
        end = min(len(lines), anchor_index + 4)
        return "\n".join(lines[start:end]).strip()[:max_chars]

    section_end = len(lines)
    for index in range(section_start + 1, len(lines)):
        heading = HEADING_RE.match(lines[index])
        if heading and len(heading.group(1)) <= section_level:
            section_end = index
            break
    return "\n".join(lines[section_start:section_end]).strip()[:max_chars]


def parse_note_images(
    note_path: Path, subject: str, content: str | None = None
) -> tuple[str, list[Source], list[str]]:
    if content is None:
        content = note_path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    heading_stack: list[tuple[int, str]] = []
    sources: list[Source] = []
    problems: list[str] = []
    seen_keys: set[tuple[str, str]] = set()

    for line_number, line in enumerate(lines):
        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            heading_stack = [item for item in heading_stack if item[0] < level]
            heading_stack.append((level, title))

        for raw_ref in iter_image_targets(line):
            image_path = resolve_image_reference(note_path, raw_ref)
            key = (relative_repo_path(note_path), relative_repo_path(image_path)) if image_path else (
                relative_repo_path(note_path), raw_ref
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            context = note_section_context(content, line_number + 1)
            if image_path is None:
                problems.append(
                    f"Markdown `{relative_repo_path(note_path)}` 第 {line_number + 1} 行的题图链接无法解析：`{raw_ref}`"
                )
            sources.append(
                Source(
                    source_id="",
                    subject=subject,
                    note_path=note_path,
                    image_path=image_path,
                    raw_image_ref=raw_ref,
                    headings=[title for _, title in heading_stack],
                    context=context,
                    line_number=line_number + 1,
                )
            )

    return content, sources, problems


def line_in_ranges(line_number: int | None, ranges: Iterable[tuple[int, int]]) -> bool:
    if line_number is None:
        return False
    return any(start <= line_number <= end for start, end in ranges)


def changed_context(
    content: str,
    line_ranges: list[tuple[int, int]],
    anchor_line: int | None = None,
) -> str:
    """返回变更行及必要的标题/锚点上下文，不返回 Markdown 全文。"""
    lines = content.splitlines()
    if not lines or not line_ranges:
        return ""

    if anchor_line is not None:
        containing = [
            line_range
            for line_range in line_ranges
            if line_range[0] <= anchor_line <= line_range[1]
        ]
        if containing:
            related_ranges = containing
        elif len(line_ranges) > 1:
            related_ranges = line_ranges
        else:
            related_ranges = [
                min(
                    line_ranges,
                    key=lambda line_range: min(
                        abs(anchor_line - line_range[0]), abs(anchor_line - line_range[1])
                    ),
                )
            ]
    else:
        related_ranges = line_ranges

    start = min(line_range[0] for line_range in related_ranges)
    end = max(line_range[1] for line_range in related_ranges)

    # 少量变更后的邻近行用于保留题图说明或变更后的下一行文字；
    # 变更前不直接带入旧正文，避免把同一文件的历史题目重新交给 AI。
    selected_indices: set[int] = set()
    local_end = min(len(lines), end + 3)
    for index in range(max(1, start), local_end + 1):
        selected_indices.add(index - 1)

    if anchor_line is not None and not any(
        line_range[0] <= anchor_line <= line_range[1]
        for line_range in related_ranges
    ):
        # 文字补充关联已有题图时，仅保留题图锚点本身和新增文字，不带入两者之间的旧总结。
        selected_indices.add(anchor_line - 1)

    first_selected_line = min(
        [index + 1 for index in selected_indices] or [start]
    )
    # 补上最近的 ##/### 标题层级；#### 题目/总结会随变更片段本身提供。
    heading_indices: list[int] = []
    for index in range(first_selected_line - 2, -1, -1):
        heading = HEADING_RE.match(lines[index])
        if heading and len(heading.group(1)) <= 3:
            heading_indices.append(index)
            if len(heading_indices) >= 3:
                break
    selected_indices.update(heading_indices)

    return "\n".join(lines[index] for index in sorted(selected_indices)).strip()


def source_key(source: Source) -> tuple[str, str]:
    return (
        relative_repo_path(source.image_path) if source.image_path else "",
        source.raw_image_ref or "",
    )


def incremental_note_sources(
    note_path: Path,
    subject: str,
    content: str,
    line_ranges: list[tuple[int, int]],
) -> tuple[list[Source], list[str]]:
    """从 Markdown 的 Git 新行范围中构造本次增量来源。"""
    if not line_ranges:
        return [], []

    content_lines = content.splitlines()
    line_ranges = [
        line_range
        for line_range in line_ranges
        if any(
            content_lines[index - 1].strip()
            for index in range(line_range[0], min(line_range[1], len(content_lines)) + 1)
            if index >= 1
        )
    ]
    if not line_ranges:
        return [], []

    _, all_sources, problems = parse_note_images(note_path, subject, content)
    result: list[Source] = []
    covered_range_indexes: set[int] = set()

    for source in all_sources:
        if source.line_number is None:
            continue
        matching_indexes = [
            index
            for index, line_range in enumerate(line_ranges)
            if line_range[0] <= source.line_number <= line_range[1]
        ]
        if not matching_indexes:
            continue
        range_index = matching_indexes[0]
        covered_range_indexes.add(range_index)
        result.append(
            replace(
                source,
                context=changed_context(
                    content, [line_ranges[range_index]], source.line_number
                ),
                change_kind="新增题目",
            )
        )

    uncovered_ranges = [
        line_range
        for index, line_range in enumerate(line_ranges)
        if index not in covered_range_indexes
    ]
    if not uncovered_ranges:
        return result, problems

    # 修改已有题目的总结时，尽量把增量片段关联到最近的已有题图；
    # 如果距离过远或本文件没有题图，则保留为纯 Markdown 来源。
    grouped_updates: dict[tuple[str, str], tuple[Source, list[tuple[int, int]]]] = {}
    unlinked_ranges: list[tuple[int, int]] = []
    for line_range in uncovered_ranges:
        nearest: Source | None = None
        if all_sources:
            nearest = min(
                all_sources,
                key=lambda source: abs(
                    (source.line_number or line_range[0]) - line_range[0]
                ),
            )
            distance = abs((nearest.line_number or line_range[0]) - line_range[0])
            if distance > 80:
                nearest = None

        if nearest is None:
            unlinked_ranges.append(line_range)
            continue

        key = source_key(nearest)
        if key not in grouped_updates:
            grouped_updates[key] = (nearest, [])
        grouped_updates[key][1].append(line_range)

    if unlinked_ranges:
        result.append(
            Source(
                source_id="",
                subject=subject,
                note_path=note_path,
                context=changed_context(content, unlinked_ranges),
                change_kind="笔记新增/修改",
            )
        )

    for nearest, update_ranges in grouped_updates.values():
        new_source = replace(
            nearest,
            context=changed_context(content, update_ranges, nearest.line_number),
            change_kind="笔记新增/修改（关联已有题图）",
        )
        existing = next(
            (source for source in result if source_key(source) == source_key(new_source)),
            None,
        )
        if existing is None:
            result.append(new_source)
        elif new_source.context and new_source.context not in existing.context:
            existing.context = f"{existing.context}\n\n---\n\n{new_source.context}"

    return result, problems


def build_subject_bundle(
    subject: str,
    changed_paths: dict[str, set[str]],
    configured_path: str,
    dirty_paths: set[str] | None = None,
    commits: Iterable[Commit] | None = None,
) -> SubjectBundle:
    prefix = configured_path.replace("\\", "/").strip("/")
    dirty_paths = {normalize_repo_path(path) for path in (dirty_paths or set())}
    subject_changed = sorted(
        path
        for path in changed_paths
        if path == prefix or path.startswith(prefix + "/")
    )
    problems: list[str] = []
    sources: list[Source] = []
    note_texts: dict[Path, str] = {}
    seen_image_paths: set[Path] = set()
    changed_images: list[Path] = []
    note_deltas = note_deltas_for_scope(commits, changed_paths) if commits else {}

    for relative_path in subject_changed:
        path = repo_path(relative_path)
        if path.suffix.lower() in IMAGE_SUFFIXES and path.exists():
            changed_images.append(path)

    for relative_path in subject_changed:
        if not relative_path.lower().endswith(".md"):
            continue
        if relative_path in dirty_paths:
            problems.append(
                f"跳过未提交的 Markdown：`{relative_path}`；本次只统计已提交内容。"
            )
            continue
        note_path = repo_path(relative_path)
        if not note_path.exists():
            problems.append(f"提交中涉及的 Markdown 当前不存在：`{relative_path}`")
            continue
        if commits is not None:
            delta = note_deltas.get(relative_path)
            if delta is None or delta.content is None:
                problems.append(
                    f"无法读取提交范围内的 Markdown 内容：`{relative_path}`；本次未纳入来源。"
                )
                continue
            content = delta.content
            note_sources, note_problems = incremental_note_sources(
                note_path, subject, content, delta.line_ranges
            )
            if not delta.line_ranges:
                statuses = ", ".join(sorted(changed_paths.get(relative_path, set())))
                problems.append(
                    f"Markdown `{relative_path}` 在本次范围内没有可提取的新增行"
                    f"（变更状态：{statuses or '未知'}）；未计为新增题目。"
                )
        else:
            content, note_sources, note_problems = parse_note_images(note_path, subject)
        note_texts[note_path] = content
        sources.extend(note_sources)
        problems.extend(note_problems)
        if not note_sources and commits is None:
            sources.append(
                Source(
                    source_id="",
                    subject=subject,
                    note_path=note_path,
                    context=content[:4000],
                    change_kind="已有笔记",
                )
            )

    clean_sources: list[Source] = []
    for source in sources:
        blocked_paths = [
            relative_repo_path(path)
            for path in (source.note_path, source.image_path)
            if path is not None and relative_repo_path(path) in dirty_paths
        ]
        if blocked_paths:
            problems.append(
                "跳过包含未提交内容的来源："
                + ", ".join(f"`{path}`" for path in blocked_paths)
                + "；本次只统计已提交内容。"
            )
            continue
        clean_sources.append(source)
    sources = clean_sources
    note_texts = {
        path: content
        for path, content in note_texts.items()
        if relative_repo_path(path) not in dirty_paths
    }

    for source in sources:
        if source.image_path:
            seen_image_paths.add(source.image_path)

    for image_path in changed_images:
        if image_path in seen_image_paths:
            continue
        image_relative = relative_repo_path(image_path)
        if image_relative in dirty_paths:
            problems.append(
                f"跳过未提交的题图：`{image_relative}`；本次只统计已提交内容。"
            )
            continue
        full_scan = commits is None
        sources.append(
            Source(
                source_id="",
                subject=subject,
                image_path=image_path,
                context=(
                    "该题图已被 Git 跟踪，但当前未在任何 Markdown 中找到对应引用。"
                    if full_scan
                    else "该题图在统计范围内被新增或修改，但当前未在本次变更的 Markdown 中找到对应引用。"
                ),
                change_kind="孤立题图" if full_scan else "题图新增/修改",
            )
        )
        if full_scan:
            problems.append(
                f"题图 `{relative_repo_path(image_path)}` 已被 Git 跟踪，但未找到任何 Markdown 引用。"
            )
        else:
            problems.append(
                f"题图 `{relative_repo_path(image_path)}` 在本次范围内新增或修改，但未找到对应的 Markdown 引用。"
            )

    sources.sort(
        key=lambda source: (
            relative_repo_path(source.note_path) if source.note_path else "",
            relative_repo_path(source.image_path) if source.image_path else "",
            source.title,
        )
    )
    for index, source in enumerate(sources, start=1):
        source.source_id = f"S{index:03d}"

    # 当前变更中存在 Markdown，但没有题图时，明确提示，不把它误判为题目已完整归档。
    for relative_path in subject_changed:
        if relative_path.lower().endswith(".md") and not repo_path(relative_path).exists():
            continue

    return SubjectBundle(subject, subject_changed, sources, problems, note_texts)


def tracked_subject_bundle(
    subject: str,
    configured_path: str,
    dirty_paths: set[str] | None = None,
) -> SubjectBundle:
    """扫描 HEAD 中已跟踪的整科资料，用于复盘和纠错。"""
    prefix = configured_path.replace("\\", "/").strip("/")
    output = run_git("-c", "core.quotePath=false", "ls-files")
    tracked = {
        normalize_repo_path(line.strip()): {"tracked"}
        for line in output.splitlines()
        if line.strip()
        and (
            normalize_repo_path(line.strip()) == prefix
            or normalize_repo_path(line.strip()).startswith(prefix + "/")
        )
    }
    return build_subject_bundle(
        subject,
        tracked,
        configured_path,
        dirty_paths=dirty_paths,
        commits=None,
    )


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
    config: dict[str, Any], dirty_paths: set[str]
) -> tuple[list[ReviewEntry], list[str]]:
    relative_path = normalize_repo_path(str(config["review"]["log_path"]))
    problems: list[str] = []
    if relative_path in dirty_paths:
        problems.append(
            f"复盘记录 `{relative_path}` 存在未提交修改；本次只读取 HEAD 中已提交的版本。"
        )
    try:
        content = read_git_file("HEAD", relative_path)
    except WorkflowError:
        problems.append(
            f"HEAD 中未找到复盘记录 `{relative_path}`；本次按没有历史记录处理。"
        )
        return [], problems
    entries, parse_problems = parse_review_log(content)
    return entries, problems + parse_problems


def review_targets_for_source(source: Source) -> set[str]:
    targets: set[str] = set()
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


def obsidian_target(path: Path) -> str:
    """返回 Obsidian 从仓库根目录解析的内部链接路径。"""
    return relative_repo_path(path)


def obsidian_link(label: str | None, target: str) -> str:
    """生成可跳转且可进入 Obsidian 关系图谱的内部链接。"""
    if label is None:
        return f"[[{target}]]"
    return f"[[{target}|{label}]]"


def source_index_markdown(bundle: SubjectBundle, report_path: Path) -> str:
    rows = [
        "| 编号 | 变更类型 | 题图 | 归纳笔记 | 知识点/题型位置 |",
        "|---|---|---|---|---|",
    ]
    for source in bundle.sources:
        if source.image_path and source.image_path.exists():
            image_cell = obsidian_link(
                None,
                obsidian_target(source.image_path),
            )
        elif source.raw_image_ref:
            image_cell = source.raw_image_ref
        else:
            image_cell = "—"
        if source.note_path and source.note_path.exists():
            note_cell = obsidian_link(
                None,
                obsidian_target(source.note_path),
            )
        else:
            note_cell = "—"
        title = source.title.replace("|", "\\|")
        change_kind = source.change_kind.replace("|", "\\|")
        rows.append(
            f"| {source.source_id} | {change_kind} | {image_cell} | {note_cell} | {title} |"
        )
    return "\n".join(rows)


def source_payload(bundle: SubjectBundle) -> str:
    parts: list[str] = []
    for source in bundle.sources:
        parts.append(f"### {source.source_id}")
        parts.append(f"- 科目：{source.subject}")
        parts.append(f"- 变更类型：{source.change_kind}")
        parts.append(f"- 题图仓库路径：{relative_repo_path(source.image_path) if source.image_path else '未解析'}")
        parts.append(f"- Markdown 路径：{relative_repo_path(source.note_path) if source.note_path else '无'}")
        parts.append(f"- 知识点/题型位置：{source.title}")
        if source.context:
            parts.append("- 相关 Markdown 上下文：")
            parts.append("```markdown")
            parts.append(source.context[:4000])
            parts.append("```")
        parts.append("")
    return "\n".join(parts)


def unique_image_paths(bundle: SubjectBundle) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for source in bundle.sources:
        if source.image_path and source.image_path.exists() and source.image_path not in seen:
            seen.add(source.image_path)
            result.append(source.image_path)
    return result


def format_commit_list(commits: Iterable[Commit]) -> str:
    rows = []
    for commit in commits:
        rows.append(
            f"- `{commit.short_sha}` {commit.message}（{commit.committed_at.strftime('%Y-%m-%d %H:%M')} 北京时间）"
        )
    return "\n".join(rows) if rows else "- 未找到符合 `daily: YYYY-MM-DD` 的提交。"


def format_changed_files(changed_paths: dict[str, set[str]]) -> str:
    rows = []
    for path in sorted(changed_paths):
        if is_ignored_source_path(path):
            continue
        statuses = ", ".join(sorted(changed_paths[path]))
        rows.append(f"- `{path}`（{statuses}）")
    return "\n".join(rows) if rows else "- 没有可纳入统计的科目文件变更。"


def daily_prompt(bundle: SubjectBundle, target_date: dt.date, config: dict[str, Any]) -> str:
    return textwrap.dedent(
        f"""
        你是考研错题整理助理。请根据下面 Git 差异提取出的 {bundle.subject} 科目新增/修改片段和题目图片，生成一段中文 Markdown 归纳，服务于 {target_date.isoformat()} 的每日复盘。

        严格规则：
        1. 输入资料已经是本次 Git 提取出的新增或修改内容；只归纳这些片段，不要根据 Markdown 路径或题图自行补回同一文件中的历史题目。
        2. 只使用输入中的题图和 Markdown；不能臆造手写草稿、答案或未提供的推导。
        3. 只总结知识点、题型、使用的方法和特殊注意事项，不代写完整解题过程。
        4. 对“新增题目”来源归纳本次新题；对“笔记新增/修改（关联已有题图）”来源，只归纳新增/修改的文字，并明确这是对已有题目的补充或复盘，不要把整道历史题重新归纳一遍。
        5. 合并重复内容，但不要漏掉本次资料体现的题型和方法。
        6. 每一条重要判断后标注来源编号，例如“（来源：S001、S002）”，只能使用输入中存在的编号。
        7. 只输出以下结构之后的内容，不要输出开场白：

        ## 知识点与题型
        - 按知识点或题型归纳，每条带来源编号。

        ## 使用的方法
        - 按方法归纳，每条带来源编号。

        ## 特殊注意事项
        - 只写输入明确支持的易错点、适用条件或检查项；不确定处写“待确认”。

        本次来源资料：
        {source_payload(bundle)}
        """
    ).strip()


def weekly_prompt(
    bundle: SubjectBundle,
    start: dt.datetime,
    end: dt.datetime,
    config: dict[str, Any],
) -> str:
    weekly = config["weekly"]
    questions = int(weekly.get("questions_per_subject", 10))
    variant_ratio = float(weekly.get("variant_ratio", 0.3))
    variant_count = round(questions * variant_ratio)
    original_count = questions - variant_count
    return textwrap.dedent(
        f"""
        你是考研周测命题与错题归纳助理。请只根据下面 Git 差异提取出的 {bundle.subject} 科目新增/修改片段，覆盖北京时间 {start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')} 期间提交的错题笔记，生成周测和过去一周的题型/方法总结。

        严格规则：
        1. 输入资料已经是本周 Git 提取出的新增或修改内容；不得根据 Markdown 路径或题图自行补回同一文件中的历史题目。
        2. 题目内容只能来自输入的 Markdown 和题图；不得引入输入之外的知识、公式结论或手写草稿内容。
        3. 先总结本周新增或修改内容体现的主要题型和方法；同类内容合并，每一类必须带来源编号。
        4. 对“笔记新增/修改（关联已有题图）”来源，只使用变更片段中的文字，不要把历史题目当作本周新题重复出题。
        5. 共生成约 {questions} 道题，目标为 {original_count} 道原题改编/直接复现、{variant_count} 道变式题。每题标注“原题”或“变式”，并带至少一个来源编号。
        6. 数学和 408 已经分开处理，本次只输出 {bundle.subject}，不要混入其他科目。
        7. 测试题部分不能出现答案；答案和核验依据必须只放在 ANSWER 标签中。
        8. 对题图文字无法辨认、原笔记缺少答案或变式无法可靠推出的地方，明确写“待确认”，不要猜测。
        9. 必须严格使用以下标签，标签名称和顺序不要改变；标签内部使用中文 Markdown：

        <SUMMARY>
        ## 过去一周总结
        ### 题型
        - 每类题型及其特点（来源：Sxxx）
        ### 方法
        - 每类方法及适用条件/提醒（来源：Sxxx）
        ### 特殊注意事项
        - ...
        </SUMMARY>
        <TEST>
        ## 测试题
        题目列表；每题标注原题/变式和来源编号，不给答案。
        </TEST>
        <ANSWER>
        ## 答案与核验
        与题号一一对应。依据仅来自来源资料；变式题说明核验思路或待确认项。
        </ANSWER>

        本次来源资料：
        {source_payload(bundle)}
        """
    ).strip()


def review_prompt(
    bundle: SubjectBundle,
    statuses: list[ReviewStatus],
    target_date: dt.date,
    config: dict[str, Any],
) -> str:
    questions = int(config["review"].get("questions_per_subject", 8))
    selected_ids = {source.source_id for source in bundle.sources}
    selected_statuses = [
        status for status in statuses if status.source.source_id in selected_ids
    ]
    return textwrap.dedent(
        f"""
        你是考研错题复盘命题助理。请根据 {bundle.subject} 科目中当前最需要复盘的题图、笔记片段和历史掌握记录，为 {target_date.isoformat()} 生成掌握度测试。

        严格规则：
        1. 共生成约 {questions} 道题，优先覆盖“薄弱、未阅读、未测试、已到期”的来源；同一来源可以从概念、条件、方法选择和易错点等不同角度检查，但不要机械重复。
        2. 只能使用输入的题图和 Markdown 所明确支持的内容。不得假设看过仓库外的手写草稿；信息不足时写“待确认”。
        3. 题目用于检查是否真正掌握，应优先检查 Markdown 总结中明确写出的知识点、适用条件、方法选择和易错点；每题必须标注来源编号。
        4. 测试题中不能泄露答案。答案、判断标准和常见错误只放在 ANSWER 标签内。
        5. 答案必须与题号一一对应。若来源不足以得到唯一答案，明确写“待确认”，不要编造。
        6. 只使用输入中存在的 S 编号，不得创建新的来源编号。
        7. 若沿用选择题，必须在测试题中完整列出所有选项；题图中的选项无法完整辨认时，改写为不依赖选项的开放题，或者明确标记“待确认”，不得只问“哪些正确”却省略选项，也不得在答案中凭空给出 A/B/C/D。
        8. 题目与答案中使用的每个条件必须来自输入。不得为了完成推导自行补充 k≠0、可逆、满秩、正定等前提。
        9. 输出前逐题自检：题干是否完整、答案是否只依赖已给条件、题号是否一一对应、结论是否能从来源核验。
        10. 必须严格使用以下标签，标签名称和顺序不要改变：
        11. 除非完整公式、选项和条件已经逐字出现在 Markdown 上下文中，否则不要从题图重新抄写长公式、具体数值或选择题选项。题图只用于理解主题；优先把题目改写为“说明方法、条件、判断依据或易错点”的开放题。

        <TEST>
        ## 掌握度测试
        题目列表；每题写明“检查目标”和来源编号，不给答案。
        </TEST>
        <ANSWER>
        ## 答案与核验
        与题号一一对应，给出简明答案、核验要点和判定为“已掌握/部分掌握/薄弱”的标准。
        </ANSWER>

        历史复盘状态：
        {review_history_payload(selected_statuses)}

        来源资料：
        {source_payload(bundle)}
        """
    ).strip()


def numbered_note_content(content: str) -> str:
    return "\n".join(
        f"{line_number:04d}: {line}"
        for line_number, line in enumerate(content.splitlines(), start=1)
    )


def correction_prompt(bundle: SubjectBundle, note_path: Path, content: str) -> str:
    return textwrap.dedent(
        f"""
        你是严谨的考研数学与 408 笔记审校员。请检查下面这篇 {bundle.subject} 笔记以及所附原始题图，找出不严谨、不完整、容易误导或事实错误的内容，并给出正确说法。

        笔记路径：{relative_repo_path(note_path)}

        审校规则：
        1. 只审校笔记中实际写出的内容，不把空白“总结/解答”、简写风格或未收录完整演算本身当成错误。
        2. 题图是原始资料，笔记是归纳；若笔记与题图冲突，应明确指出冲突。
        3. “确定错误”“表述不严谨”“待确认”必须分开。只有高置信度且可明确纠正的问题才列为确定错误。
        4. 每个问题必须给出：严重程度、Markdown 行号及标题位置、原文摘录、问题说明、正确说法、核验理由，并引用相关 S 来源；不要输出无法定位的泛泛建议。
        5. 正确说法应说明适用条件、量词、边界情形或公式前提。无法从笔记/题图确认时写“待确认”，不得假装看过手写草稿。
        6. 不代写整篇笔记，也不自动修改原文件。
        7. 只使用输入中存在的 S 编号，不得创建新编号。若某项仅来自纯文本且没有题图，可以引用对应笔记来源编号。
        8. 只输出以下中文 Markdown 结构，不要写开场白：
        9. 输出前必须独立复算每一项“正确说法”，特别检查代数展开、量词、必要/充分条件、渐近等价、级数收敛半径和端点。不能完整复算或题图条件看不清时，必须移入“待确认”，禁止用猜测补出题设。

        ## 审校结论
        - 用一至三条概括本篇笔记的可靠性和最重要风险。

        ## 确定错误
        | 严重程度 | 位置 | 原文 | 问题 | 正确说法 | 核验理由 | 来源 |
        |---|---|---|---|---|---|---|
        没有高置信度错误时写“未发现高置信度错误”。

        ## 表述不严谨
        使用同样的表格；没有时写“未发现”。

        ## 待确认
        - 只列因题图不可辨、上下文缺失或结论依赖仓库外草稿而无法核验的项目，并说明需要用户补充什么。

        带行号的笔记全文：
        ```markdown
        {numbered_note_content(content)}
        ```

        题图及来源索引：
        {source_payload(bundle)}
        """
    ).strip()


def numbered_markdown_items(value: str) -> dict[int, str]:
    matches = list(
        re.finditer(r"(?m)^\s*(?:\*\*)?(\d+)\.\s*", value)
    )
    items: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        items[int(match.group(1))] = value[match.start():end].strip()
    return items


def review_output_quality_issues(
    test: str,
    answer: str,
    valid_source_ids: set[str],
    expected_questions: int,
) -> list[str]:
    issues: list[str] = []
    test_items = numbered_markdown_items(test)
    answer_items = numbered_markdown_items(answer)
    if not test_items:
        issues.append("测试题中没有可识别的编号题目。")
    if set(test_items) != set(answer_items):
        issues.append(
            "测试题号与答案题号不一致："
            f"题目={sorted(test_items)}，答案={sorted(answer_items)}。"
        )
    if test_items and abs(len(test_items) - expected_questions) > 1:
        issues.append(
            f"题量偏离配置：期望约 {expected_questions} 题，实际 {len(test_items)} 题。"
        )
    used_ids = source_ids_in(test + "\n" + answer)
    unknown_ids = sorted(used_ids - valid_source_ids)
    if unknown_ids:
        issues.append(f"使用了不存在的来源编号：{', '.join(unknown_ids)}。")
    if not used_ids:
        issues.append("题目和答案没有引用来源编号。")

    option_pattern = re.compile(r"(?m)^\s*(?:[-*]\s*)?[A-DＡ-Ｄ][.、．)]\s*")
    answer_choice_pattern = re.compile(r"(?:选|答案(?:是|为)?[:：]?\s*)[A-DＡ-Ｄ]\b")
    for number, item in test_items.items():
        looks_like_choice = (
            "选项" in item
            or "选择正确" in item
            or "选择错误" in item
            or re.search(r"(?:下列|以下).{0,6}哪些", item)
            is not None
            or re.search(r"(?:下列|以下).{0,12}(?:说法|命题).{0,8}(?:正确|错误|成立)", item)
            is not None
        )
        if looks_like_choice and not option_pattern.search(item):
            issues.append(f"第 {number} 题看起来是选择题，但题干没有完整列出选项。")
        answer_item = answer_items.get(number, "")
        if answer_choice_pattern.search(answer_item) and not option_pattern.search(item):
            issues.append(f"第 {number} 题答案给出了选项字母，但题干没有选项。")
    return issues


def remove_numbered_markdown_items(value: str, numbers: set[int]) -> str:
    matches = list(re.finditer(r"(?m)^\s*(?:\*\*)?(\d+)\.\s*", value))
    if not matches:
        return value
    parts = [value[: matches[0].start()].rstrip()]
    for index, match in enumerate(matches):
        number = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        if number not in numbers:
            parts.append(value[match.start():end].strip())
    return "\n\n".join(part for part in parts if part).strip()


def removable_incomplete_choice_numbers(issues: list[str]) -> set[int]:
    numbers: set[int] = set()
    for issue in issues:
        if not (
            "看起来是选择题" in issue
            or "答案给出了选项字母" in issue
        ):
            return set()
        match = re.search(r"第 (\d+) 题", issue)
        if not match:
            return set()
        numbers.add(int(match.group(1)))
    return numbers


def review_repair_prompt(
    base_prompt: str,
    draft: str,
    issues: list[str],
) -> str:
    return textwrap.dedent(
        f"""
        {base_prompt}

        上一稿没有通过自动质量检查。请根据原始来源完整重写 TEST 和 ANSWER，不要解释修改过程。

        自动检查发现：
        {chr(10).join(f"- {issue}" for issue in issues)}

        上一稿：
        ```markdown
        {draft}
        ```

        修复要求：
        - 所有题都改写为开放题，不使用“下列哪些”“选项 A/B/C/D”或“选择正确/错误”等选择题表达。
        - 删除来源中不存在的附加前提。
        - 无法可靠核验的结论必须写“待确认”。
        - 保持 TEST、ANSWER 标签和题号一一对应。
        """
    ).strip()


def review_verification_prompt(
    bundle: SubjectBundle,
    draft: str,
    expected_questions: int,
) -> str:
    return textwrap.dedent(
        f"""
        你是第二轮独立命题核验员。下面是一份 {bundle.subject} 掌握度测试草稿。不要信任草稿答案，必须重新读取题图和来源片段，逐题独立计算并修订。

        核验规则：
        1. 逐字符核对公式中的正负号、指数、下标和约束条件，不能依赖草稿的转写。
        2. 严禁补充来源未给出的非零、可逆、满秩、正定等假设。尤其检查答案推导中出现、但题干和来源未出现的条件。
        3. 每道题必须仅凭题干即可作答；若题图无法辨认或条件不足，题目和答案均明确写“待确认”，不得猜测。
        4. 独立复算每个答案。若结论只在附加条件下成立，应改正结论并写明为什么原条件不足。
        5. 保持 TEST、ANSWER 标签，生成约 {expected_questions} 道题，题号一一对应；所有题使用开放题形式并引用现有 S 编号。
        6. 只输出修订后的 TEST 和 ANSWER，不解释核验过程。
        7. 如果完整题式只存在于图片而没有写入 Markdown，不得重新转写题式或给出具体计算答案；改为检查 Markdown 总结明确记录的方法、条件和易错点。

        来源资料：
        {source_payload(bundle)}

        待核验草稿：
        ```markdown
        {draft}
        ```
        """
    ).strip()


def correction_verification_prompt(
    bundle: SubjectBundle,
    note_path: Path,
    content: str,
    draft: str,
) -> str:
    return textwrap.dedent(
        f"""
        你是第二轮独立审校员。下面是一份关于 {bundle.subject} 笔记的纠错草稿。不要信任草稿中的任何数学或计算机结论，必须结合带行号原文和题图逐项重新核验，然后输出一份修订后的最终纠错报告。

        核验重点：
        1. 独立复算代数展开、矩阵维度、必要与充分条件、量词、渐近等价、级数收敛半径与端点。
        2. 草稿中只要引入了题目未给出的假设、看不清的符号或未经证明的渐近式，就不能保留在“确定错误”，应删除或移至“待确认”。
        3. 不得因为笔记简写、空白总结或未记录完整草稿就虚构“正确答案”。
        4. 每一条保留的确定错误都必须能从原文和可辨认题图直接验证；正确说法本身也必须可靠。
        5. 保持“审校结论、确定错误、表述不严谨、待确认”四段结构，只输出最终中文 Markdown，不写核验过程。
        6. 只能使用现有 S 编号。

        笔记路径：{relative_repo_path(note_path)}

        带行号的笔记全文：
        ```markdown
        {numbered_note_content(content)}
        ```

        来源：
        {source_payload(bundle)}

        待核验草稿：
        ```markdown
        {draft}
        ```
        """
    ).strip()


def mark_correction_as_unverified(value: str) -> str:
    """明确标记 AI 纠错不是已经证明的最终结论。"""
    cleaned = clean_ai_markdown(value)
    cleaned = re.sub(
        r"(?m)^##\s*确定错误\s*$",
        "## AI 候选错误（待人工确认）",
        cleaned,
    )
    cleaned = re.sub(
        r"(?m)^##\s*表述不严谨\s*$",
        "## AI 候选不严谨（待人工确认）",
        cleaned,
    )
    warning = (
        "> 质量边界：以下内容经过 AI 二次核验，但仍可能误读题图、补充不存在的题设"
        "或给出错误的“正确说法”。在对照原题、教材或可靠资料前，所有纠错项均视为候选，"
        "不得直接改写原笔记。"
    )
    return f"{warning}\n\n{cleaned}".strip()


def extract_chat_response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise WorkflowError("Chat Completions 返回中没有 choices。")
    message = choices[0].get("message", {})
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                text_value = part.get("text") or part.get("content")
                if isinstance(text_value, str):
                    chunks.append(text_value)
        if "\n".join(chunks).strip():
            return "\n".join(chunks).strip()
    if isinstance(message, dict) and message.get("reasoning_content"):
        raise WorkflowError("AI 只返回了 reasoning_content，没有返回可用的正文。")
    raise WorkflowError("Chat Completions 返回中没有可读取的正文内容。")


def chat_completions_endpoint(config: dict[str, Any]) -> str:
    explicit = os.environ.get("OPENAI_CHAT_COMPLETIONS_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
    if base_url:
        if base_url.endswith("/chat/completions"):
            return base_url
        if base_url.endswith("/v1"):
            return base_url + "/chat/completions"
        return base_url + "/v1/chat/completions"
    configured = config.get("ai", {}).get("chat_completions_url", "")
    if configured:
        return str(configured).rstrip("/")
    return "https://api.openai.com/v1/chat/completions"


def call_openai(prompt: str, bundle: SubjectBundle, config: dict[str, Any]) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise WorkflowError(
            "未找到 OPENAI_API_KEY。请设置用户环境变量，或复制自动化/.env.example 为自动化/.env 后填写。"
        )
    ai_config = config.get("ai", {})
    model = os.environ.get("OPENAI_MODEL", "").strip() or ai_config.get(
        "default_model", "claude-opus-4-8"
    )
    endpoint = chat_completions_endpoint(config)
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image_path in unique_image_paths(bundle):
        mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        source_ids = [
            source.source_id
            for source in bundle.sources
            if source.image_path == image_path
        ]
        content.append(
            {
                "type": "text",
                "text": f"以下图片对应来源编号：{', '.join(source_ids)}。请结合图片文字与前面的 Markdown。",
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{encoded}",
                },
            }
        )

    request_body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_completion_tokens": int(ai_config.get("max_output_tokens", 4096)),
        "stream": False,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "User-Agent": "daily-wrong-question/1.0",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    timeout = int(ai_config.get("timeout_seconds", 180))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            detail = parsed.get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            pass
        raise WorkflowError(f"OpenAI API 返回 HTTP {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise WorkflowError(f"无法连接 OpenAI API：{exc.reason}") from exc
    except TimeoutError as exc:
        raise WorkflowError("OpenAI API 请求超时。") from exc

    try:
        return extract_chat_response_text(json.loads(raw))
    except json.JSONDecodeError as exc:
        raise WorkflowError("OpenAI API 返回的内容不是有效 JSON。") from exc


def clean_ai_markdown(value: str) -> str:
    value = value.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def split_weekly_output(value: str) -> tuple[str, str, str]:
    value = clean_ai_markdown(value)
    tagged = {}
    for name in ("SUMMARY", "TEST", "ANSWER"):
        match = re.search(
            rf"<{name}>\s*(.*?)\s*</{name}>", value, flags=re.IGNORECASE | re.DOTALL
        )
        if match:
            tagged[name] = match.group(1).strip()
    if len(tagged) == 3:
        return tagged["SUMMARY"], tagged["TEST"], tagged["ANSWER"]

    summary_match = re.search(
        r"##\s*过去一周总结(.*?)(?=##\s*测试题|\Z)", value, flags=re.IGNORECASE | re.DOTALL
    )
    test_match = re.search(
        r"##\s*测试题(.*?)(?=##\s*答案与核验|\Z)", value, flags=re.IGNORECASE | re.DOTALL
    )
    answer_match = re.search(
        r"##\s*答案与核验(.*)", value, flags=re.IGNORECASE | re.DOTALL
    )
    if summary_match and test_match and answer_match:
        return (
            "## 过去一周总结\n" + summary_match.group(1).strip(),
            "## 测试题\n" + test_match.group(1).strip(),
            "## 答案与核验\n" + answer_match.group(1).strip(),
        )
    raise WorkflowError("周测 AI 输出缺少 SUMMARY、TEST、ANSWER 三个可分离部分。")


def split_review_output(value: str) -> tuple[str, str]:
    value = clean_ai_markdown(value)
    tagged: dict[str, str] = {}
    for name in ("TEST", "ANSWER"):
        match = re.search(
            rf"<{name}>\s*(.*?)\s*</{name}>",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            tagged[name] = match.group(1).strip()
    if len(tagged) == 2:
        return tagged["TEST"], tagged["ANSWER"]

    test_match = re.search(
        r"##\s*掌握度测试(.*?)(?=##\s*答案与核验|\Z)",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    answer_match = re.search(
        r"##\s*答案与核验(.*)", value, flags=re.IGNORECASE | re.DOTALL
    )
    if test_match and answer_match:
        return (
            "## 掌握度测试\n" + test_match.group(1).strip(),
            "## 答案与核验\n" + answer_match.group(1).strip(),
        )
    raise WorkflowError("复盘 AI 输出缺少 TEST、ANSWER 两个可分离部分。")


def link_source_ids(value: str, bundle: SubjectBundle, report_path: Path) -> str:
    """将 AI 输出中的裸来源编号变为指向题图的 Obsidian 内部链接。"""
    result = value
    for source in reversed(bundle.sources):
        target = source.image_path or source.note_path
        if not target:
            continue
        link = obsidian_target(target)
        def replace_source_id(match: re.Match[str]) -> str:
            line_start = result.rfind("\n", 0, match.start()) + 1
            line_end = result.find("\n", match.start())
            if line_end < 0:
                line_end = len(result)
            in_table_row = result[line_start:line_end].lstrip().startswith("|")
            label = None if in_table_row else source.source_id
            return obsidian_link(label, link)

        pattern = re.compile(
            rf"(?<!\[){re.escape(source.source_id)}(?!\])"
        )
        result = pattern.sub(replace_source_id, result)
    return result


def source_ids_in(value: str) -> set[str]:
    return set(SOURCE_ID_RE.findall(value))


def current_run_time() -> dt.datetime:
    return dt.datetime.now(tz=BEIJING)


def as_beijing(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING)
    return value.astimezone(BEIJING)


def scheduled_daily_date(
    schedule_time: str, now: dt.datetime | None = None
) -> dt.date:
    """计算最近一次应执行的每日统计日期，适配任务计划程序延迟唤醒。"""
    now = as_beijing(now or current_run_time())
    scheduled = dt.datetime.combine(
        now.date(), parse_clock_time(schedule_time, "daily.time"), tzinfo=BEIJING
    )
    if now < scheduled:
        return now.date() - dt.timedelta(days=1)
    return now.date()


def scheduled_weekly_end(
    weekday: str, schedule_time: str, now: dt.datetime | None = None
) -> dt.datetime:
    """计算最近一次应执行的周测结束时间，而不是使用实际唤醒时间。"""
    now = as_beijing(now or current_run_time())
    target_weekday = parse_weekday(weekday)
    days_since_target = (now.weekday() - target_weekday) % 7
    candidate_date = now.date() - dt.timedelta(days=days_since_target)
    candidate = dt.datetime.combine(
        candidate_date, parse_clock_time(schedule_time, "weekly.time"), tzinfo=BEIJING
    )
    if now < candidate:
        candidate -= dt.timedelta(days=7)
    return candidate


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise WorkflowError(f"日期格式应为 YYYY-MM-DD：{value}") from exc


def parse_datetime(value: str | None) -> dt.datetime:
    if not value:
        return current_run_time()
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WorkflowError(
            "时间格式应为 ISO 8601，例如 2026-07-26T08:00:00+08:00"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(BEIJING)


def all_commits_in_local_day(commits: Iterable[Commit], target_date: dt.date) -> list[Commit]:
    return sorted(
        [commit for commit in commits if commit.committed_at.date() == target_date],
        key=lambda commit: commit.committed_at,
    )


def write_or_preview_report(path: Path, content: str, write: bool) -> None:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return
    print(f"===== 预览：{relative_repo_path(path)} =====")
    print(content, end="" if content.endswith("\n") else "\n")


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
    bundle = tracked_subject_bundle(subject, configured_path, dirty_paths)
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

    if selected.sources and use_ai:
        try:
            base_prompt = review_prompt(selected, statuses, target_date, config)
            generated = call_openai(base_prompt, selected, config)
            test, answer = split_review_output(generated)
            valid_ids = {source.source_id for source in selected.sources}
            quality_issues = review_output_quality_issues(
                test, answer, valid_ids, question_limit
            )
            if quality_issues:
                generated = call_openai(
                    review_repair_prompt(base_prompt, generated, quality_issues),
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
                generated = call_openai(
                    review_verification_prompt(
                        selected, generated, question_limit
                    ),
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
            test = f"## 掌握度测试\n\n> AI 生成失败：{exc}"
            answer = f"## 答案与核验\n\n> 无可核验内容。原始错误：{exc}"
    elif selected.sources:
        test = "## 掌握度测试\n\n> 本次使用 `--no-ai`，未生成测试题。"
        answer = "## 答案与核验\n\n> 本次使用 `--no-ai`，未生成答案。"
    else:
        test = "## 掌握度测试\n\n> 当前没有可用于命题的已提交来源。"
        answer = "## 答案与核验\n\n> 当前没有测试题。"

    common_metadata = [
        f"> 生成时间：{current_run_time().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        f"> 复盘日期：{target_date.isoformat()}",
        "> 数据口径：读取 HEAD 中已提交的题图、笔记和复盘记录；未提交科目资料不送往外部 AI。",
        "",
    ]
    question_lines = [
        f"# 错题复盘｜{subject}｜{target_date.isoformat()}",
        "",
        *common_metadata,
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
    write_or_preview_report(
        report_path, "\n".join(question_lines).rstrip() + "\n", write
    )
    write_or_preview_report(
        answer_path, "\n".join(answer_lines).rstrip() + "\n", write
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
    entries, log_problems = load_review_log(config, dirty_paths)
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


def bundle_for_note(
    bundle: SubjectBundle, note_path: Path, content: str
) -> SubjectBundle:
    image_sources = [
        replace(source)
        for source in bundle.sources
        if source.note_path is not None and source.note_path.resolve() == note_path.resolve()
    ]
    note_sources = [
        Source(
            source_id="",
            subject=bundle.subject,
            note_path=note_path,
            headings=[note_path.stem],
            context="完整笔记文本已在本次审校输入的带行号全文中提供。",
            change_kind="完整笔记文本",
        ),
        *image_sources,
    ]
    for index, source in enumerate(note_sources, start=1):
        source.source_id = f"S{index:03d}"
        if source.image_path:
            source.change_kind = "完整笔记审校题图"
    return SubjectBundle(
        subject=bundle.subject,
        changed_paths=[relative_repo_path(note_path)],
        sources=note_sources,
        problems=[],
        note_texts={note_path: content},
    )


def correction_report_for_subject(
    subject: str,
    configured_path: str,
    target_date: dt.date,
    config: dict[str, Any],
    use_ai: bool,
    write: bool,
    dirty_paths: set[str],
) -> Path:
    bundle = tracked_subject_bundle(subject, configured_path, dirty_paths)
    report_dir = ROOT / config["reports"]["correction"]
    report_path = report_dir / f"纠错报告-{target_date.isoformat()}-{subject}.md"
    lines = [
        f"# 笔记纠错报告｜{subject}｜{target_date.isoformat()}",
        "",
        f"> 生成时间：{current_run_time().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        "> 数据口径：逐篇检查 HEAD 中已提交且工作区无未提交修改的 Markdown，并结合其引用的原始题图；报告不会自动改写笔记。",
        "",
        "## 审校范围",
        "",
        f"- 共纳入 {len(bundle.note_texts)} 篇笔记、{len(bundle.sources)} 个题目来源。",
        "",
    ]
    if bundle.problems:
        lines.extend(
            ["## 数据检查", "", *[f"- {problem}" for problem in bundle.problems], ""]
        )
    if not bundle.note_texts:
        lines.extend(["## 状态", "", "没有可审校的已提交笔记。", ""])
    for note_path, content in sorted(
        bundle.note_texts.items(), key=lambda item: relative_repo_path(item[0])
    ):
        note_bundle = bundle_for_note(bundle, note_path, content)
        note_link = obsidian_link(None, obsidian_target(note_path))
        lines.extend([f"## {note_path.stem}", "", f"- 笔记：{note_link}", ""])
        if use_ai:
            try:
                draft = call_openai(
                    correction_prompt(note_bundle, note_path, content),
                    note_bundle,
                    config,
                )
                if bool(config.get("ai", {}).get("verify_corrections", True)):
                    generated = call_openai(
                        correction_verification_prompt(
                            note_bundle, note_path, content, draft
                        ),
                        note_bundle,
                        config,
                    )
                else:
                    generated = draft
                generated = mark_correction_as_unverified(generated)
                used_ids = source_ids_in(generated)
                valid_ids = {source.source_id for source in note_bundle.sources}
                unknown_ids = sorted(used_ids - valid_ids)
                generated = link_source_ids(generated, note_bundle, report_path)
                lines.append(generated)
                if unknown_ids:
                    lines.extend(
                        [
                            "",
                            f"> 警告：AI 输出了不存在的来源编号：{', '.join(unknown_ids)}。",
                        ]
                    )
            except WorkflowError as exc:
                lines.append(f"> AI 审校失败：{exc}")
        else:
            lines.append("> 本次使用 `--no-ai`，只完成笔记、题图和链接结构检查。")
        lines.extend(
            [
                "",
                "### 本篇来源索引",
                "",
                source_index_markdown(note_bundle, report_path),
                "",
            ]
        )
    write_or_preview_report(report_path, "\n".join(lines).rstrip() + "\n", write)
    return report_path


def correction_reports(
    target_date: dt.date,
    config: dict[str, Any],
    use_ai: bool = True,
    write: bool = True,
    subject_filter: str | None = None,
) -> list[Path]:
    dirty_paths = read_uncommitted_paths()
    outputs: list[Path] = []
    subjects = config.get("subjects", {})
    if subject_filter and subject_filter not in subjects:
        raise WorkflowError(
            f"未知科目 `{subject_filter}`；可选科目：{', '.join(subjects)}"
        )
    for subject, configured_path in subjects.items():
        if subject_filter and subject != subject_filter:
            continue
        outputs.append(
            correction_report_for_subject(
                subject,
                configured_path,
                target_date,
                config,
                use_ai,
                write,
                dirty_paths,
            )
        )
    return outputs


def check_workflow(target_date: dt.date | None = None, at: dt.datetime | None = None) -> dict[str, Any]:
    config = load_config()
    commits = read_commits()
    dirty_paths = read_uncommitted_paths()
    result: dict[str, Any] = {
        "timezone": "Asia/Shanghai",
        "root": str(ROOT),
        "uncommitted_source_paths": sorted(
            path
            for path in dirty_paths
            if subject_for_path(path, config.get("subjects", {})) is not None
        ),
        "allowed_commit_examples": [
            "daily: YYYY-MM-DD",
            "weekly: YYYY-Www",
            "docs: 简短说明",
            "chore: 简短说明",
            "fix: 简短说明",
        ],
    }
    if target_date:
        daily = commits_for_daily(commits, target_date)
        changed = collect_changed_paths(daily)
        result["daily"] = {
            "date": target_date.isoformat(),
            "commits": [commit.message for commit in daily],
            "changed_paths": sorted(changed),
            "subjects": {
                subject: {
                    "sources": len(
                        build_subject_bundle(
                            subject, changed, path, dirty_paths, commits=daily
                        ).sources
                    ),
                    "problems": build_subject_bundle(
                        subject, changed, path, dirty_paths, commits=daily
                    ).problems,
                }
                for subject, path in config.get("subjects", {}).items()
            },
        }
    if at:
        start = at - dt.timedelta(days=int(config["weekly"].get("window_days", 7)))
        weekly = commits_for_week(commits, start, at)
        changed = collect_changed_paths(weekly)
        result["weekly"] = {
            "start": start.isoformat(),
            "end": at.isoformat(),
            "commits": [commit.message for commit in weekly],
            "changed_paths": sorted(changed),
        }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="每日错题自动化工作流")
    subparsers = parser.add_subparsers(dest="command", required=True)

    daily = subparsers.add_parser("daily", help="生成每日统计")
    daily.add_argument("--date", help="按指定北京时间日期运行：YYYY-MM-DD")
    daily.add_argument(
        "--scheduled",
        action="store_true",
        help="按配置中的每日时间计算最近一次应统计的日期，适合任务计划程序延迟运行",
    )
    daily.add_argument("--no-ai", action="store_true", help="不调用外部 AI，仅生成结构检查报告")
    daily.add_argument("--dry-run", action="store_true", help="生成预览到终端，不写入报告文件")

    weekly = subparsers.add_parser("weekly", help="生成数学和 408 周测")
    weekly.add_argument("--at", help="指定周测结束时间，例如 2026-07-26T08:00:00+08:00")
    weekly.add_argument(
        "--scheduled",
        action="store_true",
        help="按配置中的星期和时间计算最近一次周测结束时间，适合任务计划程序延迟运行",
    )
    weekly.add_argument("--no-ai", action="store_true", help="不调用外部 AI，仅生成无题目占位报告")
    weekly.add_argument("--dry-run", action="store_true", help="生成预览到终端，不写入报告文件")

    review = subparsers.add_parser("review", help="生成复盘状态、掌握度测试和独立答案")
    review.add_argument("--date", help="复盘日期：YYYY-MM-DD，默认使用北京时间当天")
    review.add_argument("--subject", help="只生成指定科目，例如数学或 408")
    review.add_argument("--no-ai", action="store_true", help="不调用外部 AI，仅生成追踪状态和结构报告")
    review.add_argument("--dry-run", action="store_true", help="生成预览到终端，不写入报告文件")

    correct = subparsers.add_parser("correct", help="逐篇审校笔记并生成纠错报告")
    correct.add_argument("--date", help="报告日期：YYYY-MM-DD，默认使用北京时间当天")
    correct.add_argument("--subject", help="只检查指定科目，例如数学或 408")
    correct.add_argument("--no-ai", action="store_true", help="不调用外部 AI，仅检查笔记、题图和链接结构")
    correct.add_argument("--dry-run", action="store_true", help="生成预览到终端，不写入报告文件")

    check = subparsers.add_parser("check", help="只读检查 Git 和题图/笔记解析，不写入报告")
    check.add_argument("--date", help="检查指定每日日期：YYYY-MM-DD")
    check.add_argument("--at", help="检查指定周测结束时间：ISO 8601")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config()
        if args.command == "daily":
            if args.date and args.scheduled:
                raise WorkflowError("daily 不能同时使用 --date 和 --scheduled。")
            if args.scheduled:
                target_date = scheduled_daily_date(str(config["daily"]["time"]))
            else:
                target_date = parse_date(args.date) if args.date else current_run_time().date()
            path = daily_report(
                target_date,
                config,
                use_ai=not args.no_ai,
                write=not args.dry_run,
            )
            if not args.dry_run:
                print(f"已生成：{path}")
            return 0
        if args.command == "weekly":
            if args.at and args.scheduled:
                raise WorkflowError("weekly 不能同时使用 --at 和 --scheduled。")
            if args.scheduled:
                end = scheduled_weekly_end(
                    str(config["weekly"]["weekday"]), str(config["weekly"]["time"])
                )
            else:
                end = parse_datetime(args.at)
            outputs = weekly_reports(
                end,
                config,
                use_ai=not args.no_ai,
                write=not args.dry_run,
            )
            if not args.dry_run:
                for path in outputs:
                    print(f"已生成：{path}")
            return 0
        if args.command == "review":
            target_date = (
                parse_date(args.date) if args.date else current_run_time().date()
            )
            outputs = review_reports(
                target_date,
                config,
                use_ai=not args.no_ai,
                write=not args.dry_run,
                subject_filter=args.subject,
            )
            if not args.dry_run:
                for path in outputs:
                    print(f"已生成：{path}")
            return 0
        if args.command == "correct":
            target_date = (
                parse_date(args.date) if args.date else current_run_time().date()
            )
            outputs = correction_reports(
                target_date,
                config,
                use_ai=not args.no_ai,
                write=not args.dry_run,
                subject_filter=args.subject,
            )
            if not args.dry_run:
                for path in outputs:
                    print(f"已生成：{path}")
            return 0
        if args.command == "check":
            target_date = parse_date(args.date) if args.date else None
            at = parse_datetime(args.at) if args.at else None
            print(json.dumps(check_workflow(target_date, at), ensure_ascii=False, indent=2))
            return 0
    except WorkflowError as exc:
        print(f"工作流失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 保留清晰的自动化失败出口，便于任务计划程序记录
        print(f"工作流出现未处理错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
