"""稳定题目身份索引。"""

from __future__ import annotations

import hashlib
import json
import ntpath
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .foundation import ROOT, Source, SubjectBundle, WorkflowError
from .git_store import relative_repo_path
from .repo_paths import resolve_repo_file


INDEX_PATH = ROOT / "索引" / "题目索引.json"
SCHEMA_VERSION = 1


def _digest(path: Path) -> str:
    resolved = resolve_repo_file(path)
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fallback_fingerprint(source: Source) -> str:
    parts = [
        source.subject,
        relative_repo_path(source.note_path) if source.note_path else "",
        "/".join(source.headings),
        str(source.line_number or ""),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def load_question_index(path: Path = INDEX_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "questions": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"题目索引无法读取：{path}\n{exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise WorkflowError(f"题目索引 schema_version 不受支持：{path}")
    questions = value.get("questions")
    if not isinstance(questions, list):
        raise WorkflowError("题目索引的 questions 必须是数组。")
    return value


def _records_by_hash(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["image_sha256"]): item
        for item in index.get("questions", [])
        if isinstance(item, dict) and item.get("image_sha256")
    }


def question_id_for_image(path: Path, index: dict[str, Any] | None = None) -> str:
    image_sha = _digest(path)
    existing = _records_by_hash(index or {"questions": []}).get(image_sha)
    return str(existing["question_id"]) if existing else f"q_{image_sha[:24]}"


def assign_question_ids(
    sources: Iterable[Source], index: dict[str, Any] | None = None
) -> list[Source]:
    """为来源赋予稳定 ID；优先复用索引中已有的图片哈希身份。"""

    existing = _records_by_hash(index or {"questions": []})
    assigned: list[Source] = []
    for source in sources:
        image_sha = _digest(source.image_path) if source.image_path else None
        if image_sha and image_sha in existing:
            question_id = str(existing[image_sha]["question_id"])
        else:
            fingerprint = image_sha or _fallback_fingerprint(source)
            question_id = f"q_{fingerprint[:24]}"
        source.question_id = question_id
        assigned.append(source)
    return assigned


def _source_record(source: Source) -> dict[str, Any]:
    image_path = relative_repo_path(source.image_path) if source.image_path else None
    note_path = relative_repo_path(source.note_path) if source.note_path else None
    image_sha = _digest(source.image_path) if source.image_path else None
    aliases = [value for value in (image_path, source.raw_image_ref) if value]
    return {
        "question_id": source.question_id,
        "subject": source.subject,
        "image_sha256": image_sha,
        "path_aliases": sorted(set(aliases)),
        "note_paths": [note_path] if note_path else [],
        "headings": source.headings,
    }


def _safe_aliases(values: Iterable[Any]) -> set[str]:
    return {
        str(value)
        for value in values
        if isinstance(value, str)
        and value.strip()
        and not ntpath.isabs(value)
        and not urlparse(value).scheme
    }


def build_index_records(
    bundles: Iterable[SubjectBundle], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    existing_index = existing or {"schema_version": SCHEMA_VERSION, "questions": []}
    by_id = {
        str(item["question_id"]): dict(item)
        for item in existing_index.get("questions", [])
        if isinstance(item, dict) and item.get("question_id")
    }
    for bundle in bundles:
        for source in assign_question_ids(bundle.sources, existing_index):
            record = _source_record(source)
            prior = by_id.get(source.question_id, {})
            record["path_aliases"] = sorted(
                _safe_aliases(prior.get("path_aliases", []))
                | _safe_aliases(record["path_aliases"])
            )
            record["note_paths"] = sorted(
                set(prior.get("note_paths", [])) | set(record["note_paths"])
            )
            by_id[source.question_id] = {**prior, **record}
    return {
        "schema_version": SCHEMA_VERSION,
        "questions": [by_id[key] for key in sorted(by_id)],
    }


def write_question_index(value: dict[str, Any], path: Path = INDEX_PATH) -> Path:
    from .report_io import _atomic_write

    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return path


def generate_question_index(
    config: dict[str, Any], *, dry_run: bool = True, subject_filter: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    from .source_scanner import tracked_subject_bundle
    from .git_store import read_uncommitted_paths

    subjects = config.get("subjects", {})
    if subject_filter and subject_filter not in subjects:
        raise WorkflowError(f"未知科目 `{subject_filter}`；可选科目：{', '.join(subjects)}")
    dirty_paths = read_uncommitted_paths()
    tracked_ref = str(config.get("git", {}).get("tracked_ref", "HEAD"))
    bundles = [
        tracked_subject_bundle(subject, configured_path, dirty_paths, tracked_ref)
        for subject, configured_path in subjects.items()
        if not subject_filter or subject == subject_filter
    ]
    index = build_index_records(bundles, load_question_index())
    problems = [problem for bundle in bundles for problem in bundle.problems]
    if not dry_run:
        write_question_index(index)
    return index, problems
