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
    structure_verified: bool = False,
    sources_verified: bool = False,
    domain_verified: bool = False,
    requires_answer_pair: bool = False,
    answer_pair_verified: bool = False,
    answer_leakage_free: bool = False,
) -> ValidationResult:
    values = [issue_record(issue) for issue in issues if issue]
    if not generated:
        values.append(
            issue_record(
                "没有形成可供验证的生成结果。",
                code="pipeline.generated_missing",
                severity="error",
            )
        )
    checks = (
        ("structure", structure_verified, "结构检查尚未通过。"),
        ("sources", sources_verified, "来源引用尚未完成核验。"),
        ("domain", domain_verified, "领域内容尚未经过独立核验。"),
    )
    if requires_answer_pair:
        checks += (
            ("answer_pair", answer_pair_verified, "题目与答案尚未完成配对核验。"),
            ("answer_leakage", answer_leakage_free, "尚未完成题目与答案泄漏检查。"),
        )
    for name, passed, message in checks:
        if not passed:
            values.append(
                issue_record(
                    message,
                    code=f"pipeline.validation_{name}",
                    severity="warning",
                )
            )
    frozen_values = tuple(values)
    if hard_failure:
        return ValidationResult("rejected", frozen_values)
    if frozen_values:
        return ValidationResult("needs_review", frozen_values)
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
