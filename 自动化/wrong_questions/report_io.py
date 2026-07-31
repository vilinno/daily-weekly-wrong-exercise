"""报告写入与只读预览。"""

from __future__ import annotations

from pathlib import Path

from .git_store import relative_repo_path

def write_or_preview_report(path: Path, content: str, write: bool) -> None:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return
    print(f"===== 预览：{relative_repo_path(path)} =====")
    print(content, end="" if content.endswith("\n") else "\n")
