"""AI 输出的结构校验、二次核验与风险标记。"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

from .foundation import SOURCE_ID_RE, SubjectBundle
from .git_store import relative_repo_path
from .markdown_tools import source_payload
from .prompts import numbered_note_content

def numbered_markdown_items(value: str) -> dict[int, str]:
    matches = list(
        re.finditer(r"(?m)^\s*(?:\*\*)?(\d+)\.\s*", value)
    )
    items: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        items[int(match.group(1))] = value[match.start():end].strip()
    return items

def review_output_quality_issues(
    test: str,
    answer: str,
    valid_source_ids: set[str],
    expected_questions: int,
) -> list[str]:
    issues: list[str] = []
    test_items = numbered_markdown_items(test)
    answer_items = numbered_markdown_items(answer)
    if not test_items:
        issues.append("测试题中没有可识别的编号题目。")
    if set(test_items) != set(answer_items):
        issues.append(
            "测试题号与答案题号不一致："
            f"题目={sorted(test_items)}，答案={sorted(answer_items)}。"
        )
    if test_items and abs(len(test_items) - expected_questions) > 1:
        issues.append(
            f"题量偏离配置：期望约 {expected_questions} 题，实际 {len(test_items)} 题。"
        )
    used_ids = set(SOURCE_ID_RE.findall(test + "\n" + answer))
    unknown_ids = sorted(used_ids - valid_source_ids)
    if unknown_ids:
        issues.append(f"使用了不存在的来源编号：{', '.join(unknown_ids)}。")
    if not used_ids:
        issues.append("题目和答案没有引用来源编号。")

    option_pattern = re.compile(r"(?m)^\s*(?:[-*]\s*)?[A-DＡ-Ｄ][.、．)]\s*")
    answer_choice_pattern = re.compile(r"(?:选|答案(?:是|为)?[:：]?\s*)[A-DＡ-Ｄ]\b")
    for number, item in test_items.items():
        looks_like_choice = (
            "选项" in item
            or "选择正确" in item
            or "选择错误" in item
            or re.search(r"(?:下列|以下).{0,6}哪些", item)
            is not None
            or re.search(r"(?:下列|以下).{0,12}(?:说法|命题).{0,8}(?:正确|错误|成立)", item)
            is not None
        )
        if looks_like_choice and not option_pattern.search(item):
            issues.append(f"第 {number} 题看起来是选择题，但题干没有完整列出选项。")
        answer_item = answer_items.get(number, "")
        if answer_choice_pattern.search(answer_item) and not option_pattern.search(item):
            issues.append(f"第 {number} 题答案给出了选项字母，但题干没有选项。")
    return issues


def answer_leakage_issues(test: str, answer: str) -> list[str]:
    """检查测试区是否混入答案区标记；只报告高置信度结构泄漏。"""

    issues: list[str] = []
    answer_heading = re.compile(
        r"(?im)^\s*#{1,6}\s*(?:答案|答案与核验|参考答案|解析|解答)\s*$"
    )
    if answer_heading.search(test):
        issues.append("测试题正文包含答案或解析标题，疑似发生答案泄漏。")
    if re.search(r"(?im)^\s*(?:答案|参考答案|解析)\s*[:：]", test):
        issues.append("测试题正文包含显式答案/解析字段，疑似发生答案泄漏。")
    if not answer.strip():
        issues.append("答案正文为空，无法完成答案配对核验。")
    return issues

def remove_numbered_markdown_items(value: str, numbers: set[int]) -> str:
    matches = list(re.finditer(r"(?m)^\s*(?:\*\*)?(\d+)\.\s*", value))
    if not matches:
        return value
    parts = [value[: matches[0].start()].rstrip()]
    for index, match in enumerate(matches):
        number = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        if number not in numbers:
            parts.append(value[match.start():end].strip())
    return "\n\n".join(part for part in parts if part).strip()

def removable_incomplete_choice_numbers(issues: list[str]) -> set[int]:
    numbers: set[int] = set()
    for issue in issues:
        if not (
            "看起来是选择题" in issue
            or "答案给出了选项字母" in issue
        ):
            return set()
        match = re.search(r"第 (\d+) 题", issue)
        if not match:
            return set()
        numbers.add(int(match.group(1)))
    return numbers

def review_repair_prompt(
    base_prompt: str,
    draft: str,
    issues: list[str],
) -> str:
    return textwrap.dedent(
        f"""
        {base_prompt}

        上一稿没有通过自动质量检查。请根据原始来源完整重写 TEST 和 ANSWER，不要解释修改过程。

        自动检查发现：
        {chr(10).join(f"- {issue}" for issue in issues)}

        上一稿：
        ```markdown
        {draft}
        ```

        修复要求：
        - 所有题都改写为开放题，不使用“下列哪些”“选项 A/B/C/D”或“选择正确/错误”等选择题表达。
        - 删除来源中不存在的附加前提。
        - 无法可靠核验的结论必须写“待确认”。
        - 保持 TEST、ANSWER 标签和题号一一对应。
        """
    ).strip()

def review_verification_prompt(
    bundle: SubjectBundle,
    draft: str,
    expected_questions: int,
) -> str:
    return textwrap.dedent(
        f"""
        你是第二轮独立命题核验员。下面是一份 {bundle.subject} 掌握度测试草稿。不要信任草稿答案，必须重新读取题图和来源片段，逐题独立计算并修订。

        核验规则：
        1. 逐字符核对公式中的正负号、指数、下标和约束条件，不能依赖草稿的转写。
        2. 严禁补充来源未给出的非零、可逆、满秩、正定等假设。尤其检查答案推导中出现、但题干和来源未出现的条件。
        3. 每道题必须仅凭题干即可作答；若题图无法辨认或条件不足，题目和答案均明确写“待确认”，不得猜测。
        4. 独立复算每个答案。若结论只在附加条件下成立，应改正结论并写明为什么原条件不足。
        5. 保持 TEST、ANSWER 标签，生成约 {expected_questions} 道题，题号一一对应；所有题使用开放题形式并引用现有 S 编号。
        6. 只输出修订后的 TEST 和 ANSWER，不解释核验过程。
        7. 如果完整题式只存在于图片而没有写入 Markdown，不得重新转写题式或给出具体计算答案；改为检查 Markdown 总结明确记录的方法、条件和易错点。

        来源资料：
        {source_payload(bundle)}

        待核验草稿：
        ```markdown
        {draft}
        ```
        """
    ).strip()

def correction_verification_prompt(
    bundle: SubjectBundle,
    note_path: Path,
    content: str,
    draft: str,
) -> str:
    return textwrap.dedent(
        f"""
        你是第二轮独立审校员。下面是一份关于 {bundle.subject} 笔记的纠错草稿。不要信任草稿中的任何数学或计算机结论，必须结合带行号原文和题图逐项重新核验，然后输出一份修订后的最终纠错报告。

        核验重点：
        1. 独立复算代数展开、矩阵维度、必要与充分条件、量词、渐近等价、级数收敛半径与端点。
        2. 草稿中只要引入了题目未给出的假设、看不清的符号或未经证明的渐近式，就不能保留在“确定错误”，应删除或移至“待确认”。
        3. 不得因为笔记简写、空白总结或未记录完整草稿就虚构“正确答案”。
        4. 每一条保留的确定错误都必须能从原文和可辨认题图直接验证；正确说法本身也必须可靠。
        5. 保持“审校结论、确定错误、表述不严谨、待确认”四段结构，只输出最终中文 Markdown，不写核验过程。
        6. 只能使用现有 S 编号。

        笔记路径：{relative_repo_path(note_path)}

        带行号的笔记全文：
        ```markdown
        {numbered_note_content(content)}
        ```

        来源：
        {source_payload(bundle)}

        待核验草稿：
        ```markdown
        {draft}
        ```
        """
    ).strip()

def clean_ai_markdown(value: str) -> str:
    value = value.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value

def mark_correction_as_unverified(value: str) -> str:
    """明确标记 AI 纠错不是已经证明的最终结论。"""
    cleaned = clean_ai_markdown(value)
    cleaned = re.sub(
        r"(?m)^##\s*确定错误\s*$",
        "## AI 候选错误（待人工确认）",
        cleaned,
    )
    cleaned = re.sub(
        r"(?m)^##\s*表述不严谨\s*$",
        "## AI 候选不严谨（待人工确认）",
        cleaned,
    )
    warning = (
        "> 质量边界：以下内容经过 AI 二次核验，但仍可能误读题图、补充不存在的题设"
        "或给出错误的“正确说法”。在对照原题、教材或可靠资料前，所有纠错项均视为候选，"
        "不得直接改写原笔记。"
    )
    return f"{warning}\n\n{cleaned}".strip()
