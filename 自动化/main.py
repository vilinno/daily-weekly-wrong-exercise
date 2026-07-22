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
from dataclasses import dataclass, field
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
    if not isinstance(daily, dict) or not isinstance(weekly, dict):
        raise WorkflowError("配置必须同时包含 daily 和 weekly 配置。")
    if not isinstance(subjects, dict) or not {"数学", "408"}.issubset(subjects):
        raise WorkflowError("subjects 必须同时配置数学和 408，且两科要分开生成报告。")
    if not isinstance(reports, dict) or not reports.get("daily") or not reports.get("weekly"):
        raise WorkflowError("reports 必须配置 daily 和 weekly 输出目录。")

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

    for key in ("daily", "weekly"):
        report_path = Path(str(reports[key]))
        if report_path.is_absolute() or ".." in report_path.parts:
            raise WorkflowError(f"reports.{key} 必须是仓库内的相对目录。")
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


def parse_note_images(
    note_path: Path, subject: str
) -> tuple[str, list[Source], list[str]]:
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
            context_start = max(0, line_number - 3)
            context_end = min(len(lines), line_number + 4)
            context = "\n".join(lines[context_start:context_end]).strip()
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
                )
            )

    return content, sources, problems


def build_subject_bundle(
    subject: str,
    changed_paths: dict[str, set[str]],
    configured_path: str,
    dirty_paths: set[str] | None = None,
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
        content, note_sources, note_problems = parse_note_images(note_path, subject)
        note_texts[note_path] = content
        sources.extend(note_sources)
        problems.extend(note_problems)
        if not note_sources:
            sources.append(
                Source(
                    source_id="",
                    subject=subject,
                    note_path=note_path,
                    context=content[:4000],
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
        sources.append(
            Source(
                source_id="",
                subject=subject,
                image_path=image_path,
                context="该题图在统计范围内被新增或修改，但当前未在本次变更的 Markdown 中找到对应引用。",
            )
        )
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
        "| 编号 | 题图 | 归纳笔记 | 知识点/题型位置 |",
        "|---|---|---|---|",
    ]
    for source in bundle.sources:
        if source.image_path and source.image_path.exists():
            image_cell = obsidian_link(
                None,
                obsidian_target(source.image_path),
            )
        else:
            image_cell = source.raw_image_ref or "未解析"
        if source.note_path and source.note_path.exists():
            note_cell = obsidian_link(
                None,
                obsidian_target(source.note_path),
            )
        else:
            note_cell = "—"
        title = source.title.replace("|", "\\|")
        rows.append(f"| {source.source_id} | {image_cell} | {note_cell} | {title} |")
    return "\n".join(rows)


def source_payload(bundle: SubjectBundle) -> str:
    parts: list[str] = []
    seen_notes: set[Path] = set()
    for source in bundle.sources:
        parts.append(f"### {source.source_id}")
        parts.append(f"- 科目：{source.subject}")
        parts.append(f"- 题图仓库路径：{relative_repo_path(source.image_path) if source.image_path else '未解析'}")
        parts.append(f"- Markdown 路径：{relative_repo_path(source.note_path) if source.note_path else '无'}")
        parts.append(f"- 知识点/题型位置：{source.title}")
        if source.context:
            parts.append("- 题图附近笔记片段：")
            parts.append("```markdown")
            parts.append(source.context[:4000])
            parts.append("```")
        parts.append("")

        if source.note_path and source.note_path not in seen_notes:
            seen_notes.add(source.note_path)
            content = bundle.note_texts.get(source.note_path, "")
            parts.append(f"### Markdown 全文：{relative_repo_path(source.note_path)}")
            parts.append("```markdown")
            parts.append(content[:16000])
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
        你是考研错题整理助理。请根据下面的 {bundle.subject} 科目 Markdown 笔记和题目图片，生成一段中文 Markdown 归纳，服务于 {target_date.isoformat()} 的每日复盘。

        严格规则：
        1. 只使用输入中的题图和 Markdown；不能臆造手写草稿、答案或未提供的推导。
        2. 只总结知识点、题型、使用的方法和特殊注意事项，不代写完整解题过程。
        3. 合并重复内容，但不要漏掉本次资料体现的题型和方法。
        4. 每一条重要判断后标注来源编号，例如“（来源：S001、S002）”，只能使用输入中存在的编号。
        5. 只输出以下结构之后的内容，不要输出开场白：

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
        你是考研周测命题与错题归纳助理。请只根据下面的 {bundle.subject} 科目资料，覆盖北京时间 {start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')} 期间提交的错题笔记，生成周测和过去一周的题型/方法总结。

        严格规则：
        1. 题目内容只能来自输入的 Markdown 和题图；不得引入输入之外的知识、公式结论或手写草稿内容。
        2. 先总结过去一周遇到并被归纳的全部主要题型和方法；同类内容合并，每一类必须带来源编号。
        3. 共生成约 {questions} 道题，目标为 {original_count} 道原题改编/直接复现、{variant_count} 道变式题。每题标注“原题”或“变式”，并带至少一个来源编号。
        4. 数学和 408 已经分开处理，本次只输出 {bundle.subject}，不要混入其他科目。
        5. 测试题部分不能出现答案；答案和核验依据必须只放在 ANSWER 标签中。
        6. 对题图文字无法辨认、原笔记缺少答案或变式无法可靠推出的地方，明确写“待确认”，不要猜测。
        7. 必须严格使用以下标签，标签名称和顺序不要改变；标签内部使用中文 Markdown：

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
            bundle = build_subject_bundle(subject, changed, configured_path, dirty_paths)
            lines.append(f"### {subject}")
            lines.append("")
            lines.append(
                f"- 变更文件：{len(bundle.changed_paths)} 个；来源题目：{len(bundle.sources)} 个。"
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
    bundle = build_subject_bundle(subject, changed, configured_path, dirty_paths)

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
                        build_subject_bundle(subject, changed, path, dirty_paths).sources
                    ),
                    "problems": build_subject_bundle(
                        subject, changed, path, dirty_paths
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
