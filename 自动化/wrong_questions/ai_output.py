"""AI 输出分段、来源编号校验与链接。"""

from __future__ import annotations

import re
from pathlib import Path

from .foundation import SOURCE_ID_RE, SubjectBundle, WorkflowError

from .markdown_tools import obsidian_link, obsidian_target
from .quality import clean_ai_markdown

def split_weekly_output(value: str) -> tuple[str, str, str]:
    value = clean_ai_markdown(value)
    tagged = {}
    for name in ("SUMMARY", "TEST", "ANSWER"):
        match = re.search(
            rf"<{name}>\s*(.*?)\s*</{name}>", value, flags=re.IGNORECASE | re.DOTALL
        )
        if match:
            tagged[name] = match.group(1).strip()
    if len(tagged) == 3:
        return tagged["SUMMARY"], tagged["TEST"], tagged["ANSWER"]

    summary_match = re.search(
        r"##\s*过去一周总结(.*?)(?=##\s*测试题|\Z)", value, flags=re.IGNORECASE | re.DOTALL
    )
    test_match = re.search(
        r"##\s*测试题(.*?)(?=##\s*答案与核验|\Z)", value, flags=re.IGNORECASE | re.DOTALL
    )
    answer_match = re.search(
        r"##\s*答案与核验(.*)", value, flags=re.IGNORECASE | re.DOTALL
    )
    if summary_match and test_match and answer_match:
        return (
            "## 过去一周总结\n" + summary_match.group(1).strip(),
            "## 测试题\n" + test_match.group(1).strip(),
            "## 答案与核验\n" + answer_match.group(1).strip(),
        )
    raise WorkflowError("周测 AI 输出缺少 SUMMARY、TEST、ANSWER 三个可分离部分。")

def split_review_output(value: str) -> tuple[str, str]:
    value = clean_ai_markdown(value)
    tagged: dict[str, str] = {}
    for name in ("TEST", "ANSWER"):
        match = re.search(
            rf"<{name}>\s*(.*?)\s*</{name}>",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            tagged[name] = match.group(1).strip()
    if len(tagged) == 2:
        return tagged["TEST"], tagged["ANSWER"]

    test_match = re.search(
        r"##\s*掌握度测试(.*?)(?=##\s*答案与核验|\Z)",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    answer_match = re.search(
        r"##\s*答案与核验(.*)", value, flags=re.IGNORECASE | re.DOTALL
    )
    if test_match and answer_match:
        return (
            "## 掌握度测试\n" + test_match.group(1).strip(),
            "## 答案与核验\n" + answer_match.group(1).strip(),
        )
    raise WorkflowError("复盘 AI 输出缺少 TEST、ANSWER 两个可分离部分。")

def link_source_ids(value: str, bundle: SubjectBundle, report_path: Path) -> str:
    """将 AI 输出中的裸来源编号变为指向题图的 Obsidian 内部链接。"""
    result = value
    for source in reversed(bundle.sources):
        target = source.image_path or source.note_path
        if not target:
            continue
        link = obsidian_target(target)
        def replace_source_id(match: re.Match[str]) -> str:
            line_start = result.rfind("\n", 0, match.start()) + 1
            line_end = result.find("\n", match.start())
            if line_end < 0:
                line_end = len(result)
            in_table_row = result[line_start:line_end].lstrip().startswith("|")
            label = None if in_table_row else source.source_id
            return obsidian_link(label, link)

        pattern = re.compile(
            rf"(?<!\[){re.escape(source.source_id)}(?!\])"
        )
        result = pattern.sub(replace_source_id, result)
    return result

def source_ids_in(value: str) -> set[str]:
    return set(SOURCE_ID_RE.findall(value))
