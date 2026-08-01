"""全局常量、数据模型与配置校验。"""

from __future__ import annotations

import datetime as dt
import json
import ntpath
import os
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

AUTOMATION_DIR = Path(__file__).resolve().parents[1]
ROOT = AUTOMATION_DIR.parent
CONFIG_PATH = AUTOMATION_DIR / "config.json"
ENV_PATH = AUTOMATION_DIR / ".env"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DAILY_RE = re.compile(r"^daily: (?P<date>\d{4}-\d{2}-\d{2})$")
DAILY_COMPAT_RE = re.compile(r"^daily:(?P<date>\d{4}-\d{2}-\d{2})$")
WEEKLY_RE = re.compile(r"^weekly: (?P<week>\d{4}-W\d{2})$")
ALLOWED_MESSAGE_RES = (
    DAILY_RE,
    DAILY_COMPAT_RE,
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
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
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
    is_merge: bool = False

    @property
    def short_sha(self) -> str:
        return self.sha[:8]

    @property
    def daily_date(self) -> dt.date | None:
        match = DAILY_RE.fullmatch(self.message) or DAILY_COMPAT_RE.fullmatch(self.message)
        if not match:
            return None
        try:
            return dt.date.fromisoformat(match.group("date"))
        except ValueError:
            return None

    @property
    def nonstandard_daily(self) -> bool:
        return bool(re.fullmatch(r"daily:\d{4}-\d{2}-\d{2}", self.message))

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
    question_id: str | None = None

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
    if isinstance(subjects, list):
        normalized_subjects: dict[str, str] = {}
        subject_specs: list[dict[str, Any]] = []
        for item in subjects:
            if not isinstance(item, dict) or not item.get("name") or not item.get("path"):
                raise WorkflowError("subjects 列表中的每项必须包含 name 和 path。")
            name, path = str(item["name"]).strip(), str(item["path"]).strip()
            if name in normalized_subjects:
                raise WorkflowError(f"subjects 中有重复科目：{name}")
            normalized_subjects[name] = path
            subject_specs.append(dict(item))
        subjects = normalized_subjects
        config["subjects"] = subjects
        config["subject_specs"] = subject_specs
    if not isinstance(subjects, dict) or not subjects:
        raise WorkflowError("subjects 必须是非空列表（兼容旧版对象映射）。")
    for subject, configured_path in subjects.items():
        path = str(configured_path).replace("\\", "/").strip("/")
        if not subject or not path or ntpath.isabs(str(configured_path)) or urlparse(str(configured_path)).scheme or ".." in path.split("/"):
            raise WorkflowError(f"subjects.{subject} 必须是仓库内相对目录。")
    if not isinstance(reports, dict) or not reports.get("daily") or not reports.get("weekly"):
        raise WorkflowError("reports 必须配置 daily 和 weekly 输出目录。")
    if not isinstance(review, dict):
        raise WorkflowError("配置必须包含 review 复盘配置。")

    git = config.get("git", {})
    if not isinstance(git, dict) or not str(git.get("tracked_ref", "refs/heads/main")).strip():
        raise WorkflowError("git.tracked_ref 必须是非空 Git 引用。")

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

    report_keys = ("daily", "weekly", "review", "correction", "audit")
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
