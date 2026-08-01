"""日报、周测、复盘和纠错共用的来源验证门禁。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


SOURCE_RE = re.compile(r"\bS\d{3}\b")


@dataclass(frozen=True)
class ValidationResult:
    status: str
    issues: tuple[dict[str, str], ...]


def validate_source_ids(texts: Iterable[str], valid_ids: set[str]) -> list[str]:
    used = set().union(*(set(SOURCE_RE.findall(text)) for text in texts))
    return sorted(used - valid_ids)


def validate_generated_output(
    *,
    generated: bool,
    issues: Iterable[str] = (),
    hard_failure: bool = False,
) -> ValidationResult:
    values = tuple(issue_record(issue) for issue in issues if issue)
    if hard_failure:
        return ValidationResult("rejected", values)
    if not generated or values:
        return ValidationResult("needs_review", values)
    return ValidationResult("validated", ())


def issue_record(
    issue: Any,
    *,
    code: str = "pipeline.issue",
    severity: str = "warning",
) -> dict[str, str]:
    """将历史字符串问题兼容转换为机器可读的 issue 对象。"""

    if isinstance(issue, dict):
        return {
            "code": str(issue.get("code", code)),
            "severity": str(issue.get("severity", severity)),
            "message": str(issue.get("message", "")),
        }
    if hasattr(issue, "code") and hasattr(issue, "severity") and hasattr(issue, "message"):
        return {
            "code": str(issue.code),
            "severity": str(issue.severity),
            "message": str(issue.message),
        }
    return {"code": code, "severity": severity, "message": str(issue)}
