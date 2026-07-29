"""日报、周测、复盘和纠错的 AI 提示词。"""

from __future__ import annotations

import datetime as dt
import textwrap
from pathlib import Path
from typing import Any

from .foundation import ReviewStatus, SubjectBundle
from .git_store import relative_repo_path
from .review_state import review_history_payload
from .markdown_tools import source_payload

def daily_prompt(bundle: SubjectBundle, target_date: dt.date, config: dict[str, Any]) -> str:
    return textwrap.dedent(
        f"""
        你是考研错题整理助理。请根据下面 Git 差异提取出的 {bundle.subject} 科目新增/修改片段和题目图片，生成一段中文 Markdown 归纳，服务于 {target_date.isoformat()} 的每日复盘。

        严格规则：
        1. 输入资料已经是本次 Git 提取出的新增或修改内容；只归纳这些片段，不要根据 Markdown 路径或题图自行补回同一文件中的历史题目。
        2. 只使用输入中的题图和 Markdown；不能臆造手写草稿、答案或未提供的推导。
        3. 只总结知识点、题型、使用的方法和特殊注意事项，不代写完整解题过程。
        4. 对“新增题目”来源归纳本次新题；对“笔记新增/修改（关联已有题图）”来源，只归纳新增/修改的文字，并明确这是对已有题目的补充或复盘，不要把整道历史题重新归纳一遍。
        5. 合并重复内容，但不要漏掉本次资料体现的题型和方法。
        6. 每一条重要判断后标注来源编号，例如“（来源：S001、S002）”，只能使用输入中存在的编号。
        7. 只输出以下结构之后的内容，不要输出开场白：

        ## 知识点与题型
        - 按知识点或题型归纳，每条带来源编号。

        ## 使用的方法
        - 按方法归纳，每条带来源编号。

        ## 特殊注意事项
        - 只写输入明确支持的易错点、适用条件或检查项；不确定处写“待确认”。

        本次来源资料：
        {source_payload(bundle)}
        """
    ).strip()

def weekly_prompt(
    bundle: SubjectBundle,
    start: dt.datetime,
    end: dt.datetime,
    config: dict[str, Any],
) -> str:
    weekly = config["weekly"]
    questions = int(weekly.get("questions_per_subject", 10))
    variant_ratio = float(weekly.get("variant_ratio", 0.3))
    variant_count = round(questions * variant_ratio)
    original_count = questions - variant_count
    return textwrap.dedent(
        f"""
        你是考研周测命题与错题归纳助理。请只根据下面 Git 差异提取出的 {bundle.subject} 科目新增/修改片段，覆盖北京时间 {start.strftime('%Y-%m-%d %H:%M')} 至 {end.strftime('%Y-%m-%d %H:%M')} 期间提交的错题笔记，生成周测和过去一周的题型/方法总结。

        严格规则：
        1. 输入资料已经是本周 Git 提取出的新增或修改内容；不得根据 Markdown 路径或题图自行补回同一文件中的历史题目。
        2. 题目内容只能来自输入的 Markdown 和题图；不得引入输入之外的知识、公式结论或手写草稿内容。
        3. 先总结本周新增或修改内容体现的主要题型和方法；同类内容合并，每一类必须带来源编号。
        4. 对“笔记新增/修改（关联已有题图）”来源，只使用变更片段中的文字，不要把历史题目当作本周新题重复出题。
        5. 共生成约 {questions} 道题，目标为 {original_count} 道原题改编/直接复现、{variant_count} 道变式题。每题标注“原题”或“变式”，并带至少一个来源编号。
        6. 数学和 408 已经分开处理，本次只输出 {bundle.subject}，不要混入其他科目。
        7. 测试题部分不能出现答案；答案和核验依据必须只放在 ANSWER 标签中。
        8. 对题图文字无法辨认、原笔记缺少答案或变式无法可靠推出的地方，明确写“待确认”，不要猜测。
        9. 必须严格使用以下标签，标签名称和顺序不要改变；标签内部使用中文 Markdown：

        <SUMMARY>
        ## 过去一周总结
        ### 题型
        - 每类题型及其特点（来源：Sxxx）
        ### 方法
        - 每类方法及适用条件/提醒（来源：Sxxx）
        ### 特殊注意事项
        - ...
        </SUMMARY>
        <TEST>
        ## 测试题
        题目列表；每题标注原题/变式和来源编号，不给答案。
        </TEST>
        <ANSWER>
        ## 答案与核验
        与题号一一对应。依据仅来自来源资料；变式题说明核验思路或待确认项。
        </ANSWER>

        本次来源资料：
        {source_payload(bundle)}
        """
    ).strip()

def review_prompt(
    bundle: SubjectBundle,
    statuses: list[ReviewStatus],
    target_date: dt.date,
    config: dict[str, Any],
) -> str:
    questions = int(config["review"].get("questions_per_subject", 8))
    selected_ids = {source.source_id for source in bundle.sources}
    selected_statuses = [
        status for status in statuses if status.source.source_id in selected_ids
    ]
    return textwrap.dedent(
        f"""
        你是考研错题复盘命题助理。请根据 {bundle.subject} 科目中当前最需要复盘的题图、笔记片段和历史掌握记录，为 {target_date.isoformat()} 生成掌握度测试。

        严格规则：
        1. 共生成约 {questions} 道题，优先覆盖“薄弱、未阅读、未测试、已到期”的来源；同一来源可以从概念、条件、方法选择和易错点等不同角度检查，但不要机械重复。
        2. 只能使用输入的题图和 Markdown 所明确支持的内容。不得假设看过仓库外的手写草稿；信息不足时写“待确认”。
        3. 题目用于检查是否真正掌握，应优先检查 Markdown 总结中明确写出的知识点、适用条件、方法选择和易错点；每题必须标注来源编号。
        4. 测试题中不能泄露答案。答案、判断标准和常见错误只放在 ANSWER 标签内。
        5. 答案必须与题号一一对应。若来源不足以得到唯一答案，明确写“待确认”，不要编造。
        6. 只使用输入中存在的 S 编号，不得创建新的来源编号。
        7. 若沿用选择题，必须在测试题中完整列出所有选项；题图中的选项无法完整辨认时，改写为不依赖选项的开放题，或者明确标记“待确认”，不得只问“哪些正确”却省略选项，也不得在答案中凭空给出 A/B/C/D。
        8. 题目与答案中使用的每个条件必须来自输入。不得为了完成推导自行补充 k≠0、可逆、满秩、正定等前提。
        9. 输出前逐题自检：题干是否完整、答案是否只依赖已给条件、题号是否一一对应、结论是否能从来源核验。
        10. 必须严格使用以下标签，标签名称和顺序不要改变：
        11. 除非完整公式、选项和条件已经逐字出现在 Markdown 上下文中，否则不要从题图重新抄写长公式、具体数值或选择题选项。题图只用于理解主题；优先把题目改写为“说明方法、条件、判断依据或易错点”的开放题。

        <TEST>
        ## 掌握度测试
        题目列表；每题写明“检查目标”和来源编号，不给答案。
        </TEST>
        <ANSWER>
        ## 答案与核验
        与题号一一对应，给出简明答案、核验要点和判定为“已掌握/部分掌握/薄弱”的标准。
        </ANSWER>

        历史复盘状态：
        {review_history_payload(selected_statuses)}

        来源资料：
        {source_payload(bundle)}
        """
    ).strip()

def numbered_note_content(content: str) -> str:
    return "\n".join(
        f"{line_number:04d}: {line}"
        for line_number, line in enumerate(content.splitlines(), start=1)
    )

def correction_prompt(bundle: SubjectBundle, note_path: Path, content: str) -> str:
    return textwrap.dedent(
        f"""
        你是严谨的考研数学与 408 笔记审校员。请检查下面这篇 {bundle.subject} 笔记以及所附原始题图，找出不严谨、不完整、容易误导或事实错误的内容，并给出正确说法。

        笔记路径：{relative_repo_path(note_path)}

        审校规则：
        1. 只审校笔记中实际写出的内容，不把空白“总结/解答”、简写风格或未收录完整演算本身当成错误。
        2. 题图是原始资料，笔记是归纳；若笔记与题图冲突，应明确指出冲突。
        3. “确定错误”“表述不严谨”“待确认”必须分开。只有高置信度且可明确纠正的问题才列为确定错误。
        4. 每个问题必须给出：严重程度、Markdown 行号及标题位置、原文摘录、问题说明、正确说法、核验理由，并引用相关 S 来源；不要输出无法定位的泛泛建议。
        5. 正确说法应说明适用条件、量词、边界情形或公式前提。无法从笔记/题图确认时写“待确认”，不得假装看过手写草稿。
        6. 不代写整篇笔记，也不自动修改原文件。
        7. 只使用输入中存在的 S 编号，不得创建新编号。若某项仅来自纯文本且没有题图，可以引用对应笔记来源编号。
        8. 只输出以下中文 Markdown 结构，不要写开场白：
        9. 输出前必须独立复算每一项“正确说法”，特别检查代数展开、量词、必要/充分条件、渐近等价、级数收敛半径和端点。不能完整复算或题图条件看不清时，必须移入“待确认”，禁止用猜测补出题设。

        ## 审校结论
        - 用一至三条概括本篇笔记的可靠性和最重要风险。

        ## 确定错误
        | 严重程度 | 位置 | 原文 | 问题 | 正确说法 | 核验理由 | 来源 |
        |---|---|---|---|---|---|---|
        没有高置信度错误时写“未发现高置信度错误”。

        ## 表述不严谨
        使用同样的表格；没有时写“未发现”。

        ## 待确认
        - 只列因题图不可辨、上下文缺失或结论依赖仓库外草稿而无法核验的项目，并说明需要用户补充什么。

        带行号的笔记全文：
        ```markdown
        {numbered_note_content(content)}
        ```

        题图及来源索引：
        {source_payload(bundle)}
        """
    ).strip()
