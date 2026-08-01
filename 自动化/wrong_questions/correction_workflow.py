"""笔记纠错候选报告工作流。"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .foundation import ROOT, Source, SubjectBundle, WorkflowError
from .git_store import read_uncommitted_paths, relative_repo_path, resolve_commit
from .source_scanner import tracked_subject_bundle
from .markdown_tools import obsidian_link, obsidian_target, source_index_markdown
from .prompts import correction_prompt
from .quality import correction_verification_prompt, mark_correction_as_unverified
from .ai_client import call_openai
from .ai_output import link_source_ids, source_ids_in
from .scheduling import current_run_time
from .report_io import workflow_entry, write_or_preview_report
from .run_metadata import run_metadata_block
from .pipeline_validation import validate_source_ids
from .pipeline_validation import validate_generated_output

def bundle_for_note(
    bundle: SubjectBundle, note_path: Path, content: str
) -> SubjectBundle:
    image_sources = [
        replace(source)
        for source in bundle.sources
        if source.note_path is not None and source.note_path.resolve() == note_path.resolve()
    ]
    note_sources = [
        Source(
            source_id="",
            subject=bundle.subject,
            note_path=note_path,
            headings=[note_path.stem],
            context="完整笔记文本已在本次审校输入的带行号全文中提供。",
            change_kind="完整笔记文本",
        ),
        *image_sources,
    ]
    for index, source in enumerate(note_sources, start=1):
        source.source_id = f"S{index:03d}"
        if source.image_path:
            source.change_kind = "完整笔记审校题图"
    return SubjectBundle(
        subject=bundle.subject,
        changed_paths=[relative_repo_path(note_path)],
        sources=note_sources,
        problems=[],
        note_texts={note_path: content},
    )

def correction_report_for_subject(
    subject: str,
    configured_path: str,
    target_date: dt.date,
    config: dict[str, Any],
    use_ai: bool,
    write: bool,
    dirty_paths: set[str],
) -> Path:
    tracked_ref = str(config.get("git", {}).get("tracked_ref", "refs/heads/main"))
    snapshot_commit = resolve_commit(tracked_ref)
    bundle = tracked_subject_bundle(subject, configured_path, dirty_paths, tracked_ref)
    report_dir = ROOT / config["reports"]["correction"]
    report_path = report_dir / f"纠错报告-{target_date.isoformat()}-{subject}.md"
    run_id = uuid.uuid4().hex
    question_ids = [source.question_id for source in bundle.sources]
    prompts: list[str] = []
    pipeline_issues = list(bundle.problems)
    generation_failed = False
    lines = [
        f"# 笔记纠错报告｜{subject}｜{target_date.isoformat()}",
        "",
        f"> 生成时间：{current_run_time().strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        "> 数据口径：逐篇检查 tracked_ref 指向提交中且工作区无未提交修改的 Markdown，并结合其引用的原始题图；报告不会自动改写笔记。",
        "",
        "## 审校范围",
        "",
        f"- 共纳入 {len(bundle.note_texts)} 篇笔记、{len(bundle.sources)} 个题目来源。",
        "",
    ]
    if bundle.problems:
        lines.extend(
            ["## 数据检查", "", *[f"- {problem}" for problem in bundle.problems], ""]
        )
    if not bundle.note_texts:
        lines.extend(["## 状态", "", "没有可审校的已提交笔记。", ""])
    for note_path, content in sorted(
        bundle.note_texts.items(), key=lambda item: relative_repo_path(item[0])
    ):
        note_bundle = bundle_for_note(bundle, note_path, content)
        note_link = obsidian_link(None, obsidian_target(note_path))
        lines.extend([f"## {note_path.stem}", "", f"- 笔记：{note_link}", ""])
        if use_ai:
            try:
                prompt = correction_prompt(note_bundle, note_path, content)
                prompts.append(prompt)
                draft = call_openai(
                    prompt,
                    note_bundle,
                    config,
                )
                if bool(config.get("ai", {}).get("verify_corrections", True)):
                    verification_prompt = correction_verification_prompt(
                        note_bundle, note_path, content, draft
                    )
                    prompts.append(verification_prompt)
                    generated = call_openai(
                        verification_prompt,
                        note_bundle,
                        config,
                    )
                else:
                    generated = draft
                generated = mark_correction_as_unverified(generated)
                valid_ids = {source.source_id for source in note_bundle.sources}
                unknown_ids = validate_source_ids([generated], valid_ids)
                generated = link_source_ids(generated, note_bundle, report_path)
                lines.append(generated)
                if unknown_ids:
                    generation_failed = True
                    pipeline_issues.append(
                        f"{note_path.stem} AI 输出了不存在的来源编号：{', '.join(unknown_ids)}"
                    )
                    lines.extend(
                        [
                            "",
                            f"> 警告：AI 输出了不存在的来源编号：{', '.join(unknown_ids)}。",
                        ]
                    )
            except WorkflowError as exc:
                generation_failed = True
                pipeline_issues.append(f"{note_path.stem} AI 审校失败：{exc}")
                lines.append(f"> AI 审校失败：{exc}")
        else:
            pipeline_issues.append(f"{note_path.stem} 使用 --no-ai，未生成可验证纠错结论。")
            lines.append("> 本次使用 `--no-ai`，只完成笔记、题图和链接结构检查。")
        lines.extend(
            [
                "",
                "### 本篇来源索引",
                "",
                source_index_markdown(note_bundle, report_path),
                "",
            ]
        )
    validation = validate_generated_output(
        generated=bool(prompts) and not generation_failed,
        issues=pipeline_issues,
        hard_failure=generation_failed,
        structure_verified=bool(prompts) and not generation_failed,
        sources_verified=bool(prompts) and not generation_failed,
        domain_verified=False,
    )
    status = validation.status
    metadata_issues = [
        *pipeline_issues,
        *[
            item["message"]
            for item in validation.issues
            if item["message"] not in pipeline_issues
        ],
    ]
    lines.extend(
        ["", run_metadata_block(
            kind="correction", status=status, config=config,
            question_ids=question_ids, prompts=prompts,
            issues=metadata_issues, run_id=run_id,
            snapshot_commit=snapshot_commit, scope_kind="snapshot",
        ), ""]
    )
    write_or_preview_report(report_path, "\n".join(lines).rstrip() + "\n", write)
    return report_path

@workflow_entry
def correction_reports(
    target_date: dt.date,
    config: dict[str, Any],
    use_ai: bool = True,
    write: bool = True,
    subject_filter: str | None = None,
) -> list[Path]:
    dirty_paths = read_uncommitted_paths()
    outputs: list[Path] = []
    subjects = config.get("subjects", {})
    if subject_filter and subject_filter not in subjects:
        raise WorkflowError(
            f"未知科目 `{subject_filter}`；可选科目：{', '.join(subjects)}"
        )
    for subject, configured_path in subjects.items():
        if subject_filter and subject != subject_filter:
            continue
        outputs.append(
            correction_report_for_subject(
                subject,
                configured_path,
                target_date,
                config,
                use_ai,
                write,
                dirty_paths,
            )
        )
    return outputs
