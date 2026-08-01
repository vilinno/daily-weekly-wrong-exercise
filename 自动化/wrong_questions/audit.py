"""全仓库只读审计：发现问题并输出结构化 findings，不自动修复资料。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import ntpath
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .foundation import HEADING_RE, IMAGE_SUFFIXES, ROOT, WorkflowError
from .question_index import load_question_index
from .repo_paths import resolve_repo_file, resolve_repo_image
from .source_scanner import iter_image_targets
from .report_io import _atomic_write, workflow_lock


INLINE_CODE_RE = re.compile(r"`[^`\r\n]*`")


@dataclass(frozen=True)
class AuditFinding:
    code: str
    severity: str
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditResult:
    status: str
    generated_at: str
    findings: list[AuditFinding]
    summary: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at,
            "summary": self.summary,
            "findings": [asdict(item) for item in self.findings],
        }


def classify_reference(raw_ref: str) -> str:
    value = raw_ref.strip()
    if not value:
        return "empty"
    if ntpath.isabs(value) or Path(value).is_absolute():
        return "absolute"
    if urlparse(value).scheme or value.startswith(("//", "\\\\")):
        return "remote"
    return "relative"


def _relative(path: Path, root: Path = ROOT) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def _finding(
    code: str,
    severity: str,
    message: str,
    path: Path | str | None = None,
    **details: Any,
) -> AuditFinding:
    return AuditFinding(
        code=code,
        severity=severity,
        message=message,
        path=_relative(path) if isinstance(path, Path) else path,
        details=details,
    )


def audit_markdown_references(note_path: Path, content: str) -> list[AuditFinding]:
    """审计一篇 Markdown 的图片引用；纯函数边界便于 fixture 测试。"""

    findings: list[AuditFinding] = []
    in_fence = False
    for line_number, line in enumerate(content.splitlines(), start=1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # 文档中的行内代码示例不是实际图片引用，例如 README 中的 `![[文件名.png]]`。
        for raw_ref in iter_image_targets(INLINE_CODE_RE.sub("", line)):
            kind = classify_reference(raw_ref)
            if kind == "remote":
                findings.append(
                    _finding(
                        "markdown.image_remote",
                        "error",
                        f"Markdown 图片引用使用远程地址：`{raw_ref}`。",
                        note_path,
                        line=line_number,
                        reference=raw_ref,
                    )
                )
                continue
            if kind == "absolute":
                findings.append(
                    _finding(
                        "markdown.image_absolute",
                        "error",
                        f"Markdown 图片引用使用绝对路径：`{raw_ref}`。",
                        note_path,
                        line=line_number,
                        reference=raw_ref,
                    )
                )
                continue
            try:
                resolve_repo_image(
                    raw_ref,
                    base_dirs=(
                        note_path.parent,
                        note_path.parent / "assets",
                        note_path.parent.parent / "assets",
                    ),
                    unique_basename_fallback=True,
                )
            except WorkflowError as exc:
                code = "markdown.path_escape" if "逃逸" in str(exc) or ".." in raw_ref.replace("\\", "/").split("/") else "markdown.image_broken"
                findings.append(
                    _finding(
                        code,
                        "error",
                        f"Markdown 图片链接无法解析：`{raw_ref}`（{exc}）。",
                        note_path,
                        line=line_number,
                        reference=raw_ref,
                    )
                )
    return findings


def _subject_map(config: dict[str, Any]) -> dict[str, str]:
    subjects = config.get("subjects", {})
    if isinstance(subjects, list):
        return {str(item["name"]): str(item["path"]) for item in subjects}
    return {str(name): str(path) for name, path in subjects.items()}


def _subject_roots(config: dict[str, Any], root: Path) -> dict[str, Path]:
    return {
        subject: (root / Path(*configured.replace("\\", "/").strip("/").split("/"))).resolve()
        for subject, configured in _subject_map(config).items()
    }


def _configured_report_roots(config: dict[str, Any], root: Path) -> dict[str, Path]:
    reports = config.get("reports", {})
    return {
        str(key): (root / Path(*str(value).replace("\\", "/").strip("/").split("/"))).resolve()
        for key, value in reports.items()
        if value
    }


def _question_section_has_source(content: str, heading_line: int, heading_level: int) -> bool:
    lines = content.splitlines()
    start = heading_line
    boundary = len(lines)
    for index in range(start, len(lines)):
        match = HEADING_RE.match(lines[index])
        if match and len(match.group(1)) <= heading_level:
            boundary = index
            break
    section = "\n".join(lines[start:boundary])
    return any(iter_image_targets(INLINE_CODE_RE.sub("", section))) or bool(
        re.search(r"(?im)question\s*id\s*[：:]\s*[Qq]-|题目\s*ID\s*[：:]\s*[Qq]-", section)
    )


def _image_files(subject_roots: dict[str, Path]) -> list[Path]:
    paths: list[Path] = []
    for subject_root in subject_roots.values():
        if not subject_root.exists():
            continue
        paths.extend(
            path
            for path in subject_root.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        )
    return sorted(set(path.resolve() for path in paths))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _report_status(text: str, suffix: str) -> str | None:
    if suffix == ".json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
        return str(value.get("status")) if isinstance(value, dict) and value.get("status") else None
    match = re.search(r"(?:status|状态)\s*[：:]\s*`?([a-z_]+|validated|needs_review|rejected)", text, re.I)
    return match.group(1) if match else None


def audit_repository(
    config: dict[str, Any], *, root: Path = ROOT, index_path: Path | None = None
) -> AuditResult:
    root = root.resolve()
    findings: list[AuditFinding] = []
    subject_roots = _subject_roots(config, root)

    # 配置目录冲突与仓库外链接。
    for subject, subject_root in subject_roots.items():
        if not subject_root.exists():
            findings.append(_finding("subject.missing_directory", "error", f"科目目录不存在：{subject_root}。", subject_root))
    subjects = list(subject_roots.items())
    for index, (left_name, left_root) in enumerate(subjects):
        for right_name, right_root in subjects[index + 1 :]:
            if left_root == right_root or left_root in right_root.parents or right_root in left_root.parents:
                findings.append(_finding("subject.directory_conflict", "error", f"科目目录重叠：{left_name} 与 {right_name}。", _relative(left_root, root), subjects=[left_name, right_name]))
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_symlink():
            try:
                resolve_repo_file(path, must_exist=False, must_be_file=False)
            except WorkflowError as exc:
                findings.append(_finding("path.outside_symlink", "error", f"符号链接或 junction 指向仓库外：{exc}。", _relative(path, root)))

    # 过渡站积压。
    staging = root / "过渡站"
    staged_files = [path for path in staging.rglob("*") if path.is_file()] if staging.exists() else []
    if staged_files:
        findings.append(_finding("staging.backlog", "warning", f"过渡站仍有 {len(staged_files)} 个待整理文件。", _relative(staging, root), count=len(staged_files)))

    markdown_files = [
        path for path in root.rglob("*.md")
        if ".git" not in path.parts and "__pycache__" not in path.parts
    ]
    source_markdown_files = [
        path
        for path in markdown_files
        if any(path == subject_root or subject_root in path.parents for subject_root in subject_roots.values())
    ]
    report_roots = _configured_report_roots(config, root)
    referenced_images: set[Path] = set()
    for note_path in markdown_files:
        try:
            content = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            findings.append(_finding("markdown.unreadable", "error", f"Markdown 无法读取：{exc}。", _relative(note_path, root)))
            continue
        findings.extend(audit_markdown_references(note_path, content))
        for line in content.splitlines():
            if note_path not in source_markdown_files:
                break
            for raw_ref in iter_image_targets(line):
                if classify_reference(raw_ref) != "relative":
                    continue
                try:
                    referenced_images.add(
                        resolve_repo_image(
                            raw_ref,
                            base_dirs=(
                                note_path.parent,
                                note_path.parent / "assets",
                                note_path.parent.parent / "assets",
                            ),
                            unique_basename_fallback=True,
                        )
                    )
                except WorkflowError:
                    pass
        for line_number, line in enumerate(content.splitlines(), start=1):
            heading = HEADING_RE.match(line)
            if heading and heading.group(2).strip().startswith("题目"):
                if not _question_section_has_source(
                    content, line_number, len(heading.group(1))
                ):
                    findings.append(_finding("question.heading_without_source", "warning", "题目标题没有发现题图来源，可能缺少 Question ID。", _relative(note_path, root), line=line_number))

    image_files = _image_files(subject_roots)
    for image_path in image_files:
        if image_path not in referenced_images:
            findings.append(_finding("image.orphan", "warning", "题图未被任何 Markdown 引用。", _relative(image_path, root)))

    by_name: dict[str, list[Path]] = defaultdict(list)
    by_hash: dict[str, list[Path]] = defaultdict(list)
    for image_path in image_files:
        by_name[image_path.name.casefold()].append(image_path)
        try:
            by_hash[_sha256(image_path)].append(image_path)
        except OSError as exc:
            findings.append(_finding("image.unreadable", "error", f"题图无法读取：{exc}。", _relative(image_path, root)))
    for name, paths in by_name.items():
        if len(paths) > 1:
            findings.append(_finding("image.duplicate_filename", "warning", f"题图文件名重复：`{name}`。", paths=[_relative(path, root) for path in paths]))
    for digest, paths in by_hash.items():
        if len(paths) > 1:
            findings.append(_finding("image.duplicate_hash", "warning", f"题图内容哈希重复：`{digest[:16]}`。", paths=[_relative(path, root) for path in paths]))

    # Question ID 索引与题目身份核对。
    actual_hashes: dict[str, Path] = {}
    for image_path in image_files:
        try:
            actual_hashes[_sha256(image_path)] = image_path
        except OSError:
            continue
    try:
        index = load_question_index(index_path or (root / "索引" / "题目索引.json"))
        records = index.get("questions", [])
        id_counts = Counter(str(item.get("question_id")) for item in records if isinstance(item, dict))
        for question_id, count in id_counts.items():
            if not question_id or count > 1:
                findings.append(_finding("index.duplicate_id", "error", f"Question ID 重复或为空：`{question_id}`。", "索引/题目索引.json", count=count))
        seen_hashes: dict[str, str] = {}
        for item in records:
            if not isinstance(item, dict):
                continue
            question_id = str(item.get("question_id", ""))
            raw_image_sha = item.get("image_sha256")
            image_sha = str(raw_image_sha) if raw_image_sha else ""
            if image_sha and image_sha in seen_hashes and seen_hashes[image_sha] != question_id:
                findings.append(_finding("index.duplicate_image_identity", "error", f"同一图片哈希对应多个 Question ID：`{image_sha[:16]}`。", "索引/题目索引.json"))
            if image_sha:
                seen_hashes[image_sha] = question_id
            if image_sha and image_sha not in actual_hashes:
                findings.append(_finding("index.orphan_record", "warning", f"索引记录找不到对应题图：`{question_id}`。", "索引/题目索引.json", image_sha256=image_sha))
            for alias in item.get("path_aliases", []) if isinstance(item.get("path_aliases", []), list) else []:
                kind = classify_reference(str(alias))
                if kind in {"absolute", "remote"}:
                    findings.append(_finding("index.external_alias", "error", f"索引包含外部路径别名：`{alias}`。", "索引/题目索引.json", question_id=question_id))
        indexed_hashes = {
            str(item.get("image_sha256"))
            for item in records
            if isinstance(item, dict) and item.get("image_sha256")
        }
        for image_path in referenced_images:
            try:
                image_sha = _sha256(image_path)
            except OSError:
                continue
            if image_sha not in indexed_hashes:
                findings.append(_finding("question.missing_id", "warning", "Markdown 已引用题图，但索引中没有对应 Question ID。", _relative(image_path, root), image_sha256=image_sha))
    except WorkflowError as exc:
        findings.append(_finding("index.invalid", "error", f"Question ID 索引不可用：{exc}。", "索引/题目索引.json"))

    # 报告状态和未验证报告。
    audit_root = report_roots.get("audit", root / Path("报告", "审计"))
    report_metadata: dict[Path, str | None] = {}
    for report_name, report_root in report_roots.items():
        if report_name == "audit":
            continue
        if not report_root.exists():
            continue
        for report_path in report_root.rglob("*"):
            if not report_path.is_file() or report_path.suffix.lower() not in {".md", ".json"}:
                continue
            if audit_root in report_path.parents:
                continue
            try:
                report_text = report_path.read_text(encoding="utf-8", errors="replace")
                status = _report_status(report_text, report_path.suffix.lower())
                run_match = re.search(r'"run_id"\s*:\s*"([^"]+)"|run_id\s*[：:]\s*`?([^`\s]+)', report_text)
                report_metadata[report_path] = next(
                    (value for value in run_match.groups() if value), None
                ) if run_match else None
            except OSError:
                status = None
                report_metadata[report_path] = None
            if status is None:
                findings.append(_finding("report.unverified", "warning", "报告没有机器可读的验证状态。", _relative(report_path, root)))
            elif status != "validated":
                findings.append(_finding("report.not_validated", "warning", f"报告状态为 `{status}`，不是 validated。", _relative(report_path, root), status=status))

    report_files = set(report_metadata)
    for report_path in sorted(report_files):
        if report_path.name.endswith("-答案.md"):
            question_path = report_path.with_name(report_path.name[:-len("-答案.md")] + ".md")
        elif report_path.suffix.lower() == ".md" and report_path.name.startswith("周测-"):
            question_path = report_path.with_name(report_path.stem + "-答案.md")
        elif report_path.suffix.lower() == ".md" and report_path.name.startswith("复盘-"):
            question_path = report_path.with_name(report_path.stem + "-答案.md")
        else:
            continue
        if question_path not in report_files:
            continue
        left_run = report_metadata.get(report_path)
        right_run = report_metadata.get(question_path)
        if left_run and right_run and left_run != right_run:
            findings.append(
                _finding(
                    "report.pair_run_mismatch",
                    "error",
                    "题目报告与答案报告的 run_id 不一致，疑似不是同一次成对写入。",
                    _relative(report_path, root),
                    paired_path=_relative(question_path, root),
                    left_run_id=left_run,
                    right_run_id=right_run,
                )
            )

    summary = dict(Counter(item.code for item in findings))
    if any(item.severity == "error" for item in findings):
        status = "rejected"
    elif findings:
        status = "needs_review"
    else:
        status = "validated"
    return AuditResult(status, dt.datetime.now(dt.timezone.utc).isoformat(), findings, summary)


def audit_markdown(result: AuditResult) -> str:
    lines = [
        "# 全仓库审计报告",
        "",
        f"- status：`{result.status}`",
        f"- generated_at：`{result.generated_at}`",
        f"- findings：{len(result.findings)} 条",
        "",
        "## 汇总",
        "",
    ]
    lines.extend(f"- `{code}`：{count}" for code, count in sorted(result.summary.items()))
    if not result.summary:
        lines.append("- 无问题。")
    lines.extend(["", "## Findings", "", "| 严重级别 | code | 路径 | 说明 |", "|---|---|---|---|"])
    for item in result.findings:
        message = item.message.replace("|", "\\|")
        lines.append(f"| {item.severity} | `{item.code}` | `{item.path or ''}` | {message} |")
    return "\n".join(lines) + "\n"


def write_audit_reports(result: AuditResult, json_path: Path, markdown_path: Path) -> None:
    with workflow_lock():
        _atomic_write(json_path, json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n")
        _atomic_write(markdown_path, audit_markdown(result))
