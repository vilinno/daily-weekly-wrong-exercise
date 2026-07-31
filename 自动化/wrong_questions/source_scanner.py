"""Markdown、题图与增量来源解析。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote

from .foundation import Commit, HEADING_RE, IMAGE_RE, IMAGE_SUFFIXES, OBSIDIAN_IMAGE_RE, ROOT, Source, SubjectBundle
from .git_store import normalize_repo_path, note_deltas_for_scope, relative_repo_path, repo_path, run_git

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
