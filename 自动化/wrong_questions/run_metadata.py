"""统一流水线运行元数据与状态渲染。"""

from __future__ import annotations

import hashlib
import os
import uuid
import json
from typing import Any, Iterable

from .foundation import WorkflowError
from .git_store import resolve_commit
from .pipeline_validation import issue_record


PIPELINE_VERSION = "2"


def prompt_digest(prompts: Iterable[str]) -> str:
    values = [value for value in prompts if value]
    if not values:
        return "未生成"
    payload = "\n\n---\n\n".join(values).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def run_metadata_block(
    *,
    kind: str,
    status: str,
    config: dict,
    question_ids: Iterable[str | None] = (),
    prompts: Iterable[str] = (),
    issues: Iterable[str] = (),
    run_id: str | None = None,
    base_commit: str | None = None,
    tip_commit: str | None = None,
    source_commits: Iterable[str] = (),
    snapshot_commit: str | None = None,
    scope_kind: str | None = None,
    ai_calls: Iterable[dict[str, Any]] = (),
    model_rechecked: bool = False,
) -> str:
    payload = run_metadata_payload(
        kind=kind,
        status=status,
        config=config,
        question_ids=question_ids,
        prompts=prompts,
        issues=issues,
        run_id=run_id,
        base_commit=base_commit,
        tip_commit=tip_commit,
        source_commits=source_commits,
        snapshot_commit=snapshot_commit,
        scope_kind=scope_kind,
        ai_calls=ai_calls,
        model_rechecked=model_rechecked,
    )
    display_issues = payload["issues"]
    lines = [
        "## 运行元数据",
        "",
        f"- pipeline：`collect → generate → validate_structure → validate_sources → validate_domain → render`（v{PIPELINE_VERSION}）",
        f"- run_id：`{payload['run_id']}`",
        f"- kind：`{payload['kind']}`",
        f"- status：`{payload['status']}`",
        f"- tracked_ref：`{payload['tracked_ref']}`",
        f"- base_commit：`{payload['base_commit']}`",
        f"- tip_commit：`{payload['tip_commit']}`",
        f"- snapshot_commit：`{payload['snapshot_commit']}`",
        f"- source_commits：`{', '.join(payload['source_commits']) if payload['source_commits'] else '无'}`",
        f"- generation_model：`{payload['generation_model']}`",
        f"- verification_model：`{payload['verification_model']}`",
        f"- domain_verification：`{payload['domain_verification']}`",
        f"- ai_calls：`{len(payload['ai_calls'])}`",
        f"- prompt_version：`{payload['prompt_version']}`",
        f"- prompt_sha256：`{payload['prompt_sha256']}`",
        f"- question_ids：`{', '.join(payload['question_ids']) if payload['question_ids'] else '无'}`",
    ]
    if display_issues:
        lines.extend(
            ["- issues："]
            + [f"  - `{item['code']}`/{item['severity']}：{item['message']}" for item in display_issues]
        )
    else:
        lines.append("- issues：无")
    lines.extend(["", "```json", json.dumps(payload, ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines)


def run_metadata_payload(
    *,
    kind: str,
    status: str,
    config: dict,
    question_ids: Iterable[str | None] = (),
    prompts: Iterable[str] = (),
    issues: Iterable[Any] = (),
    run_id: str | None = None,
    base_commit: str | None = None,
    tip_commit: str | None = None,
    source_commits: Iterable[str] = (),
    snapshot_commit: str | None = None,
    scope_kind: str | None = None,
    ai_calls: Iterable[dict[str, Any]] = (),
    model_rechecked: bool = False,
) -> dict[str, Any]:
    if status not in {"validated", "needs_review", "rejected"}:
        raise WorkflowError(f"未知流水线状态：{status}")
    if scope_kind not in {None, "range", "snapshot"}:
        raise WorkflowError(f"未知 Git 范围类型：{scope_kind}")
    tracked_ref = str(config.get("git", {}).get("tracked_ref", "refs/heads/main"))
    prompt_values = tuple(value for value in prompts if value)
    normalized_ai_calls = [
        {
            "role": str(call.get("role", "")),
            "provider": str(call.get("provider", "")),
            "model": str(call.get("model", "")),
            "endpoint": str(call.get("endpoint", "")),
            "request_id": call.get("request_id"),
        }
        for call in ai_calls
        if isinstance(call, dict) and call.get("model")
    ]

    def actual_models(role: str) -> str:
        models = list(
            dict.fromkeys(
                call["model"]
                for call in normalized_ai_calls
                if call["role"] == role and call["model"]
            )
        )
        if models:
            return "；".join(models)
        return "未确认" if prompt_values else "未调用"

    generation_model = actual_models("generation")
    verification_model = actual_models("verification")
    ids = sorted({value for value in question_ids if value})
    if scope_kind == "range":
        actual_base = base_commit
        actual_tip = tip_commit
        actual_snapshot = None
    elif scope_kind == "snapshot":
        actual_base = None
        actual_tip = snapshot_commit or resolve_commit(tracked_ref)
        actual_snapshot = actual_tip
    else:
        actual_base = base_commit
        actual_tip = tip_commit
        actual_snapshot = snapshot_commit
        if actual_base is None and actual_tip is None and actual_snapshot is None:
            actual_tip = resolve_commit(tracked_ref)
            actual_snapshot = actual_tip
    commits = list(dict.fromkeys(value for value in source_commits if value))
    return {
        "run_id": run_id or uuid.uuid4().hex,
        "kind": kind,
        "status": status,
        "tracked_ref": tracked_ref,
        "scope_kind": scope_kind or ("snapshot" if actual_snapshot else "range"),
        "base_commit": actual_base,
        "tip_commit": actual_tip,
        "snapshot_commit": actual_snapshot,
        "source_commits": commits,
        "generation_model": generation_model if prompt_values else "未调用",
        "verification_model": verification_model if prompt_values else "未调用",
        "domain_verification": "model_rechecked" if model_rechecked else "not_run",
        "ai_calls": normalized_ai_calls,
        "prompt_version": config.get("pipeline", {}).get("prompt_version", "v1"),
        "prompt_sha256": prompt_digest(prompt_values),
        "question_ids": ids,
        "issues": [issue_record(issue) for issue in issues if issue],
    }
