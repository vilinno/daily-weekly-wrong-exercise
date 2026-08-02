"""稳定题目身份索引。

Question ID 只在索引写入时生成一次。图片哈希、路径别名和语义指纹都只是
匹配元数据，不再直接充当永久 ID；出现多候选或重复图片时必须人工确认。
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from .foundation import ROOT, Source, SubjectBundle, WorkflowError
from .git_store import read_git_bytes, relative_repo_path, resolve_commit
from .repo_paths import resolve_repo_file


INDEX_PATH = ROOT / "索引" / "题目索引.json"
SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
QuestionIdFactory = Callable[[Source, str | None], str]


def _digest(path: Path, revision: str | None = None) -> str:
    if revision:
        data = read_git_bytes(revision, relative_repo_path(path))
        return hashlib.sha256(data).hexdigest()
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
        if (
            alias
            and not ntpath.isabs(alias)
            and not urlparse(alias).scheme
            and ".." not in alias.split("/")
        ):
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
    record.setdefault("first_indexed_commit", record.pop("first_seen_commit", None))
    record.setdefault("last_indexed_commit", record.pop("last_seen_commit", None))
    record["path_aliases"] = sorted(_safe_aliases(record.get("path_aliases", [])))
    record["note_paths"] = sorted(_safe_aliases(record.get("note_paths", [])))
    # 读取旧索引时兼容历史字段，但对外只暴露不夸大历史精度的新名称。
    record.pop("first_seen_commit", None)
    record.pop("last_seen_commit", None)
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
    conflicts = value.get("conflicts", [])
    return {
        "schema_version": SCHEMA_VERSION,
        "questions": records,
        "conflicts": conflicts if isinstance(conflicts, list) else [],
    }


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


def _match_evidence(
    source: Source,
    image_sha: str | None,
    index: dict[str, Any],
) -> dict[str, Any]:
    """返回按证据类型拆分的匹配结果，避免单个旧路径静默继承新题 ID。"""

    by_id = _records_by_id(index)
    if source.question_id and source.question_id in by_id:
        return {
            "image_sha": image_sha,
            "alias_ids": set(),
            "image_ids": set(),
            "identity_ids": set(),
            "matches": [by_id[source.question_id]],
            "conflict": None,
        }
    aliases = {_normalize_text(value) for value in _source_aliases(source)}
    alias_records = _records_by_value(index, "path_aliases", normalize=_normalize_text)
    alias_matches = [
        item for alias in aliases for item in alias_records.get(alias, [])
    ]
    image_matches = (
        _records_by_value(index, "image_sha256").get(image_sha, [])
        if image_sha
        else []
    )
    identity = _identity_key(source)
    identity_matches = _records_by_value(index, "identity_key").get(identity, [])

    alias_ids = {str(item["question_id"]) for item in alias_matches}
    image_ids = {str(item["question_id"]) for item in image_matches}
    identity_ids = {str(item["question_id"]) for item in identity_matches}
    conflict: str | None = None
    selected_ids: set[str] = set()
    if len(image_ids) > 1 or len(alias_ids) > 1:
        conflict = "来源证据匹配到多个已有 Question ID"
    elif image_ids and alias_ids and image_ids != alias_ids:
        conflict = "图片哈希与路径别名指向不同的已有 Question ID"
    elif image_ids:
        # 图片哈希是强证据；同一标题下的其他题目不能因为共享章节标题而干扰它。
        selected_ids = set(image_ids)
    elif alias_ids:
        selected_ids = set(alias_ids)
    elif len(identity_ids) > 1:
        conflict = "语义证据匹配到多个已有 Question ID"
    elif identity_ids:
        selected_ids = set(identity_ids)
    if len(alias_ids) == 1 and not image_ids:
        alias_record = next(item for item in alias_matches if item.get("question_id"))
        old_image_sha = alias_record.get("image_sha256")
        old_identity = alias_record.get("identity_key")
        if (
            image_sha
            and old_image_sha
            and image_sha != old_image_sha
            and old_identity
            and identity != old_identity
        ):
            conflict = "路径别名复用但图片和语义证据同时变化"
    return {
        "image_sha": image_sha,
        "alias_ids": alias_ids,
        "image_ids": image_ids,
        "identity_ids": identity_ids,
        "matches": [by_id[question_id] for question_id in sorted(selected_ids)],
        "conflict": conflict,
    }


def question_id_for_image(
    path: Path,
    index: dict[str, Any] | None = None,
    revision: str | None = None,
) -> str:
    """从已持久化索引解析题图 ID；未登记或冲突时拒绝猜测。"""

    image_sha = _digest(path, revision)
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
    conflicts: list[dict[str, Any]] | None = None,
) -> list[Source]:
    """按索引匹配来源；新增 ID 只允许在显式生成索引时创建。"""

    value = index or {"schema_version": SCHEMA_VERSION, "questions": []}
    source_list = list(sources)
    observations: list[tuple[Source, dict[str, Any]]] = []
    for source in source_list:
        image_sha = _digest(source.image_path, source.image_revision) if source.image_path else None
        observations.append((source, _match_evidence(source, image_sha, value)))

    # 同一轮首建中多个未登记来源共享相同图片/语义时，不猜测它们是否是同一道题。
    new_groups: dict[str, list[Source]] = {}
    for source, evidence in observations:
        if evidence["conflict"]:
            continue
        if not evidence["matches"]:
            key = evidence["image_sha"] or f"identity:{_identity_key(source)}"
            new_groups.setdefault(key, []).append(source)
    duplicate_new_keys = {key for key, group in new_groups.items() if len(group) > 1}
    assigned: list[Source] = []
    claimed: dict[str, str] = {}
    for source, evidence in observations:
        image_sha = evidence["image_sha"]
        ids = sorted(
            {
                str(item["question_id"])
                for item in evidence["matches"]
                if item.get("question_id")
            }
        )
        new_key = image_sha or f"identity:{_identity_key(source)}"
        if evidence["conflict"] or len(ids) > 1 or (
            not ids and new_key in duplicate_new_keys
        ):
            source.question_id = None
            if problems is not None:
                reason = evidence["conflict"] or (
                    "本次首建中多个来源共享同一图片/语义证据"
                )
                problems.append(
                    f"来源 `{source.title}` 无法自动分配 Question ID（未登记来源）："
                    f"{reason}；待人工确认。"
                )
            if conflicts is not None:
                conflicts.append(
                    {
                        "status": "conflict",
                        "reason": evidence["conflict"] or "duplicate_new_evidence",
                        "source": source.title,
                        "image_sha256": image_sha,
                        "path_aliases": sorted(_source_aliases(source)),
                    }
                )
            assigned.append(source)
            continue
        question_id: str | None = ids[0] if ids else None
        if image_sha and question_id:
            prior_claim = claimed.get(image_sha)
            if prior_claim and prior_claim != question_id:
                question_id = None
                if problems is not None:
                    problems.append(
                        f"图片哈希 `{image_sha[:12]}` 在本次来源中出现多个身份候选；待人工确认。"
                    )
            else:
                claimed[image_sha] = question_id
        if question_id is None and create_missing:
            question_id = id_factory(source, image_sha)
            if image_sha:
                claimed[image_sha] = question_id
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
    image_sha = (
        _digest(source.image_path, source.image_revision)
        if source.image_path
        else None
    )
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
        "first_indexed_commit": snapshot_commit,
        "last_indexed_commit": snapshot_commit,
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
        "conflicts": (
            existing_index.get("conflicts", [])
            if isinstance(existing_index.get("conflicts", []), list)
            else []
        ),
    }
    by_id = _records_by_id(existing_index)
    local_problems: list[str] = []
    local_conflicts: list[dict[str, Any]] = []
    for bundle in bundles:
        sources = assign_question_ids(
            bundle.sources,
            existing_index,
            create_missing=True,
            id_factory=id_factory,
            problems=local_problems,
            conflicts=local_conflicts,
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
            record["first_indexed_commit"] = (
                prior.get("first_indexed_commit") or snapshot_commit
            )
            record["last_indexed_commit"] = snapshot_commit or prior.get(
                "last_indexed_commit"
            )
            by_id[source.question_id] = {**prior, **record}
    if problems is not None:
        problems.extend(local_problems)
    result = {
        "schema_version": SCHEMA_VERSION,
        "questions": [by_id[key] for key in sorted(by_id)],
    }
    if local_conflicts:
        result["conflicts"] = local_conflicts
    validate_index_invariants(result)
    return result


def validate_index_invariants(index: dict[str, Any]) -> None:
    """阻止把相互矛盾的永久身份写入索引。"""

    seen_ids: set[str] = set()
    image_ids: dict[str, set[str]] = {}
    alias_ids: dict[str, set[str]] = {}
    for record in index.get("questions", []):
        if not isinstance(record, dict) or not record.get("question_id"):
            raise WorkflowError("题目索引包含缺少 question_id 的正式记录。")
        question_id = str(record["question_id"])
        if question_id in seen_ids:
            raise WorkflowError(f"题目索引中 Question ID 重复：{question_id}")
        seen_ids.add(question_id)
        if str(record.get("status", "active")) != "active":
            continue
        image_sha = record.get("image_sha256")
        if image_sha:
            image_ids.setdefault(str(image_sha), set()).add(question_id)
        for alias in _safe_aliases(record.get("path_aliases", [])):
            alias_ids.setdefault(alias, set()).add(question_id)
    duplicate_images = {
        value: ids for value, ids in image_ids.items() if len(ids) > 1
    }
    if duplicate_images:
        raise WorkflowError(
            "题目索引中同一图片哈希映射多个 active Question ID："
            + "; ".join(f"{value[:12]}={sorted(ids)}" for value, ids in duplicate_images.items())
        )
    duplicate_aliases = {
        value: ids for value, ids in alias_ids.items() if len(ids) > 1
    }
    if duplicate_aliases:
        raise WorkflowError(
            "题目索引中同一路径别名映射多个 active Question ID："
            + "; ".join(f"{value}={sorted(ids)}" for value, ids in duplicate_aliases.items())
        )


def write_question_index(value: dict[str, Any], path: Path = INDEX_PATH) -> Path:
    from .report_io import _atomic_write

    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return path


def generate_question_index(
    config: dict[str, Any], *, dry_run: bool = True, subject_filter: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    from .source_scanner import tracked_subject_bundle
    from .git_store import read_uncommitted_paths
    from .report_io import workflow_lock

    lock_context = nullcontext() if dry_run else workflow_lock()
    with lock_context:
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
        # 资料扫描阶段会提示“尚未登记”，但 index 命令本身正是登记动作；
        # 保留真实路径、内容和冲突问题，避免把预期的首建提示误报为失败。
        problems = [
            problem
            for bundle in bundles
            for problem in bundle.problems
            if "尚未登记 Question ID" not in problem
        ]
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
