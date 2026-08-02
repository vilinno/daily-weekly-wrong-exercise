"""报告中的 Obsidian 链接、来源索引与文本格式化。"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .foundation import Commit, SubjectBundle
from .git_store import is_ignored_source_path, relative_repo_path

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
        "| 编号 | Question ID | 变更类型 | 题图 | 归纳笔记 | 知识点/题型位置 |",
        "|---|---|---|---|---|---|",
    ]
    for source in bundle.sources:
        if source.image_path:
            image_cell = obsidian_link(
                None,
                obsidian_target(source.image_path),
            )
        elif source.raw_image_ref:
            image_cell = source.raw_image_ref
        else:
            image_cell = "—"
        if source.note_path:
            note_cell = obsidian_link(
                None,
                obsidian_target(source.note_path),
            )
        else:
            note_cell = "—"
        title = source.title.replace("|", "\\|")
        change_kind = source.change_kind.replace("|", "\\|")
        rows.append(
            f"| {source.source_id} | {source.question_id or '待生成'} | {change_kind} | {image_cell} | {note_cell} | {title} |"
        )
    return "\n".join(rows)

def source_payload(bundle: SubjectBundle) -> str:
    parts: list[str] = []
    for source in bundle.sources:
        parts.append(f"### {source.source_id}｜{source.question_id or '待生成题目 ID'}")
        parts.append(f"- 科目：{source.subject}")
        parts.append(f"- 变更类型：{source.change_kind}")
        parts.append(f"- 题图仓库路径：{relative_repo_path(source.image_path) if source.image_path else '未解析'}")
        if source.image_revision:
            parts.append(f"- 题图读取提交：`{source.image_revision}`")
        parts.append(f"- Markdown 路径：{relative_repo_path(source.note_path) if source.note_path else '无'}")
        if source.note_revision:
            parts.append(f"- Markdown 读取提交：`{source.note_revision}`")
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
