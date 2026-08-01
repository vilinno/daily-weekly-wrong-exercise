"""稳定题目身份索引。

Question ID 只在索引写入时生成一次。图片哈希、路径别名和语义指纹都只是
匹配元数据，不再直接充当永久 ID；出现多候选或重复图片时必须人工确认。
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from .foundation import ROOT, Source, SubjectBundle, WorkflowError
from .git_store import relative_repo_path, resolve_commit
from .repo_paths import resolve_repo_file


INDEX_PATH = ROOT / "索引" / "题目索引.json"
SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
QuestionIdFactory = Callable[[Source, str | None], str]


def _digest(path: Path) -> str:
    resolved = resolve_repo_file(path)
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _identity_key(source: Source) -> str:
    """不包含路径和行号的语义候选键，允许笔记移动或插入行后继续匹配。"""

    parts = [source.subject, *source.headings]
    if not source.headings:
        parts.append(source.title)
    return hashlib.sha256(
        "\x1f".join(_normalize_text(part) for part in parts).encode("utf-8")
    ).hexdigest()


def _safe_aliases(values: Iterable[Any]) -> set[str]:
    aliases: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        alias = value.replace("\\", "/").strip()
        while alias.startswith("./"):
            alias = alias[2:]
        if alias and not ntpath.isabs(alias) and not urlparse(alias).scheme:
            aliases.add(alias)
    return aliases


def _source_aliases(source: Source) -> set[str]:
    values = []
    if source.image_path:
        values.append(relative_repo_path(source.image_path))
    if source.raw_image_ref:
        values.append(source.raw_image_ref)
    return _safe_aliases(values)


def _new_question_id(_: Source, __: str | None) -> str:
    return f"Q-{uuid.uuid4().hex}"


def _provisional_question_id(source: Source, image_sha: str | None) -> str:
    digest = image_sha or _identity_key(source)
    return f"Q-PENDING-{digest[:20]}"


def _migrate_record(value: dict[str, Any]) -> dict[str, Any]:
    record = dict(value)
    record.setdefault("status", "active")
    record.setdefault("path_aliases", [])
    record.setdefault("note_paths", [])
    record.setdefault("headings", [])
    record.setdefault("identity_key", None)
    record.setdefault("first_seen_commit", None)
    record.setdefault("last_seen_commit", None)
    record["path_aliases"] = sorted(_safe_aliases(record.get("path_aliases", [])))
    record["note_paths"] = sorted(_safe_aliases(record.get("note_paths", [])))
    return record


def load_question_index(path: Path = INDEX_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "questions": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"题目索引无法读取：{path}\n{exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") not in {
        LEGACY_SCHEMA_VERSION,
        SCHEMA_VERSION,
    }:
        raise WorkflowError(f"题目索引 schema_version 不受支持：{path}")
    questions = value.get("questions")
    if not isinstance(questions, list):
        raise WorkflowError("题目索引的 questions 必须是数组。")
    records = [
        _migrate_record(item)
        for item in questions
        if isinstance(item, dict) and item.get("question_id")
    ]
    return {"schema_version": SCHEMA_VERSION, "questions": records}


def _records_by_id(index: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["question_id"]): item
        for item in index.get("questions", [])
        if isinstance(item, dict) and item.get("question_id")
    }


def _records_by_value(
    index: dict[str, Any], field: str, *, normalize: Callable[[str], str] = lambda value: value
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for item in index.get("questions", []):
        if not isinstance(item, dict) or not item.get("question_id"):
            continue
        values = item.get(field, []) if isinstance(item.get(field), list) else [item.get(field)]
        for value in values:
            if value:
                result.setdefault(normalize(str(value)), []).append(item)
    return result


def _match_candidates(
    source: Source,
    image_sha: str | None,
    index: dict[str, Any],
) -> list[dict[str, Any]]:
    by_id = _records_by_id(index)
    if source.question_id and source.question_id in by_id:
        return [by_id[source.question_id]]

    candidates: dict[str, dict[str, Any]] = {}
    if image_sha:
        for item in _records_by_value(index, "image_sha256").get(image_sha, []):
            candidates[str(item["question_id"])] = item
    aliases = {_normalize_text(value) for value in _source_aliases(source)}
    alias_records = _records_by_value(
        index, "path_aliases", normalize=_normalize_text
    )
    for alias in aliases:
        for item in alias_records.get(alias, []):
            candidates[str(item["question_id"])] = item

    identity = _identity_key(source)
    for item in _records_by_value(index, "identity_key").get(identity, []):
        candidates[str(item["question_id"])] = item
    return list(candidates.values())


def question_id_for_image(path: Path, index: dict[str, Any] | None = None) -> str:
    """从已持久化索引解析题图 ID；未登记或冲突时拒绝猜测。"""

    image_sha = _digest(path)
    value = index if index is not None else load_question_index()
    matches = _records_by_value(value, "image_sha256").get(image_sha, [])
    ids = sorted({str(item["question_id"]) for item in matches})
    if len(ids) == 1:
        return ids[0]
    if len(ids) > 1:
        raise WorkflowError(
            f"题图哈希对应多个 Question ID：{', '.join(ids)}；请先处理索引冲突。"
        )
    raise WorkflowError(
        "题图尚未登记到持久化 Question ID 索引；请先运行 `index` 并提交索引。"
    )


def assign_question_ids(
    sources: Iterable[Source],
    index: dict[str, Any] | None = None,
    *,
    create_missing: bool = False,
    id_factory: QuestionIdFactory = _new_question_id,
    problems: list[str] | None = None,
) -> list[Source]:
    """按索引匹配来源；新增 ID 只允许在显式生成索引时创建。"""

    value = index or {"schema_version": SCHEMA_VERSION, "questions": []}
    claimed: dict[str, str] = {}
    assigned: list[Source] = []
    for source in sources:
        image_sha = _digest(source.image_path) if source.image_path else None
        matches = _match_candidates(source, image_sha, value)
        ids = sorted({str(item["question_id"]) for item in matches})
        if len(ids) > 1:
            source.question_id = None
            if problems is not None:
                problems.append(
                    f"来源 `{source.title}` 匹配到多个 Question ID：{', '.join(ids)}；待人工确认。"
                )
            assigned.append(source)
            continue
        question_id: str | None = ids[0] if ids else None
        if image_sha and question_id:
            prior_claim = claimed.get(image_sha)
            if prior_claim:
                question_id = None
                if problems is not None:
                    problems.append(
                        f"图片哈希 `{image_sha[:12]}` 在本次来源中出现多个身份候选；待人工确认。"
                    )
            else:
                claimed[image_sha] = question_id
        if question_id is None and create_missing:
            question_id = id_factory(source, image_sha)
        if question_id is None and problems is not None:
            problems.append(
                f"来源 `{source.title}` 尚未登记 Question ID；请运行 `index` 生成持久索引。"
            )
        source.question_id = question_id
        assigned.append(source)
    return assigned


def _source_record(
    source: Source,
    *,
    snapshot_commit: str | None,
) -> dict[str, Any]:
    image_path = relative_repo_path(source.image_path) if source.image_path else None
    note_path = relative_repo_path(source.note_path) if source.note_path else None
    image_sha = _digest(source.image_path) if source.image_path else None
    aliases = _source_aliases(source)
    return {
        "question_id": source.question_id,
        "status": "active",
        "subject": source.subject,
        "image_sha256": image_sha,
        "path_aliases": sorted(aliases),
        "note_paths": [note_path] if note_path else [],
        "headings": list(source.headings),
        "identity_key": _identity_key(source),
        "first_seen_commit": snapshot_commit,
        "last_seen_commit": snapshot_commit,
    }


def build_index_records(
    bundles: Iterable[SubjectBundle],
    existing: dict[str, Any] | None = None,
    *,
    snapshot_commit: str | None = None,
    id_factory: QuestionIdFactory = _new_question_id,
    problems: list[str] | None = None,
) -> dict[str, Any]:
    existing_index = existing or {"schema_version": SCHEMA_VERSION, "questions": []}
    existing_index = {
        "schema_version": SCHEMA_VERSION,
        "questions": [_migrate_record(item) for item in existing_index.get("questions", [])],
    }
    by_id = _records_by_id(existing_index)
    local_problems: list[str] = []
    for bundle in bundles:
        sources = assign_question_ids(
            bundle.sources,
            existing_index,
            create_missing=True,
            id_factory=id_factory,
            problems=local_problems,
        )
        for source in sources:
            if not source.question_id:
                continue
            record = _source_record(source, snapshot_commit=snapshot_commit)
            prior = by_id.get(source.question_id, {})
            record["path_aliases"] = sorted(
                _safe_aliases(prior.get("path_aliases", []))
                | _safe_aliases(record["path_aliases"])
            )
            record["note_paths"] = sorted(
                _safe_aliases(prior.get("note_paths", []))
                | _safe_aliases(record["note_paths"])
            )
            record["first_seen_commit"] = (
                prior.get("first_seen_commit") or snapshot_commit
            )
            record["last_seen_commit"] = snapshot_commit or prior.get("last_seen_commit")
            by_id[source.question_id] = {**prior, **record}
    if problems is not None:
        problems.extend(local_problems)
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
    tracked_ref = str(config.get("git", {}).get("tracked_ref", "refs/heads/main"))
    snapshot_commit = resolve_commit(tracked_ref)
    bundles = [
        tracked_subject_bundle(subject, configured_path, dirty_paths, tracked_ref)
        for subject, configured_path in subjects.items()
        if not subject_filter or subject == subject_filter
    ]
    problems = [problem for bundle in bundles for problem in bundle.problems]
    factory = _provisional_question_id if dry_run else _new_question_id
    index = build_index_records(
        bundles,
        load_question_index(),
        snapshot_commit=snapshot_commit,
        id_factory=factory,
        problems=problems,
    )
    if not dry_run:
        write_question_index(index)
    return index, problems
