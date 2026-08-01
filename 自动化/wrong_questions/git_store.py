"""Git 提交、差异与已提交文件读取。"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path
from typing import Iterable

from .foundation import ALLOWED_MESSAGE_RES, BEIJING, Commit, DIFF_HUNK_RE, NoteDelta, ROOT, WorkflowError
from .repo_paths import resolve_repo_file

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

def read_commits(tracked_ref: str = "HEAD") -> list[Commit]:
    """只读取指定 ref 的祖先链，不扫描 --all。"""
    ref = tracked_ref.strip() or "HEAD"
    output = run_git("log", ref, "--format=%H%x09%cI%x09%P%x09%s")
    commits: list[Commit] = []
    for line in output.splitlines():
        fields = line.split("\t", 3)
        if len(fields) != 4:
            continue
        sha, committed_at, parents, message = fields
        commits.append(
            Commit(
                sha,
                parse_git_datetime(committed_at),
                message,
                is_merge=len(parents.split()) > 1,
            )
        )
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
    safe_path = resolve_repo_file(
        normalize_repo_path(relative_path), must_exist=False, must_be_file=False
    ).relative_to(ROOT.resolve()).as_posix()
    return run_git(
        "-c",
        "core.quotePath=false",
        "show",
        f"{commit_sha}:{safe_path}",
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
        if commit.is_merge:
            continue
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
        if commit.nonstandard_daily:
            issues.append(
                f"提交 {commit.short_sha} 使用兼容但不规范的 message：`{commit.message}`；"
                "建议改为 `daily: YYYY-MM-DD`。"
            )
    return issues

def commits_for_daily(commits: Iterable[Commit], target_date: dt.date) -> list[Commit]:
    return sorted(
        [
            commit for commit in commits
            if not commit.is_merge and commit.daily_date == target_date
        ],
        key=lambda commit: commit.committed_at,
    )

def commits_for_week(
    commits: Iterable[Commit], start: dt.datetime, end: dt.datetime
) -> list[Commit]:
    return sorted(
        [
            commit
            for commit in commits
            if not commit.is_merge
            and commit.daily_date is not None
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
    return resolve_repo_file(relative_path, must_exist=False, must_be_file=False)

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
