"""统一流水线运行元数据与状态渲染。"""

from __future__ import annotations

import hashlib
import os
import uuid
import json
from typing import Any, Iterable

from .foundation import WorkflowError
from .git_store import run_git
from .pipeline_validation import issue_record


PIPELINE_VERSION = "1"


def _revision(ref: str, suffix: str = "") -> str:
    try:
        return run_git("rev-parse", f"{ref}{suffix}").strip()
    except WorkflowError:
        return "未知"


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
) -> str:
    payload = run_metadata_payload(
        kind=kind,
        status=status,
        config=config,
        question_ids=question_ids,
        prompts=prompts,
        issues=issues,
        run_id=run_id,
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
        f"- generation_model：`{payload['generation_model']}`",
        f"- verification_model：`{payload['verification_model']}`",
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
) -> dict[str, Any]:
    if status not in {"validated", "needs_review", "rejected"}:
        raise WorkflowError(f"未知流水线状态：{status}")
    tracked_ref = str(config.get("git", {}).get("tracked_ref", "HEAD"))
    ai = config.get("ai", {})
    generation_model = os.environ.get("OPENAI_MODEL", "").strip() or str(
        ai.get("default_model", "未配置")
    )
    verification_model = os.environ.get("OPENAI_VERIFY_MODEL", "").strip() or str(
        ai.get("verify_model", generation_model)
    )
    ids = sorted({value for value in question_ids if value})
    return {
        "run_id": run_id or uuid.uuid4().hex,
        "kind": kind,
        "status": status,
        "tracked_ref": tracked_ref,
        "base_commit": _revision(tracked_ref, "^"),
        "tip_commit": _revision(tracked_ref),
        "generation_model": generation_model if prompts else "未调用",
        "verification_model": verification_model if prompts else "未调用",
        "prompt_version": config.get("pipeline", {}).get("prompt_version", "v1"),
        "prompt_sha256": prompt_digest(prompts),
        "question_ids": ids,
        "issues": [issue_record(issue) for issue in issues if issue],
    }
