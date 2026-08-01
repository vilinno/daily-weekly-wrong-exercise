"""报告写入与只读预览。"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import tempfile
from typing import Iterator

from .git_store import relative_repo_path
from .foundation import ROOT, WorkflowError


LOCK_PATH = ROOT / "报告" / ".workflow.lock"


@contextlib.contextmanager
def workflow_lock() -> Iterator[None]:
    """以仓库内独占锁保护同一时间的报告/答案生成。"""

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise WorkflowError(f"已有自动化运行正在写入报告：{relative_repo_path(LOCK_PATH)}") from exc
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(f"pid={os.getpid()}\n")
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass


def write_report_pair(
    first_path: Path,
    first_content: str,
    second_path: Path,
    second_content: str,
    write: bool,
) -> None:
    """周测/复盘题目与答案在同一把锁内成对写入。"""

    if not write:
        write_or_preview_report(first_path, first_content, False)
        write_or_preview_report(second_path, second_content, False)
        return
    with workflow_lock():
        _atomic_write(first_path, first_content)
        _atomic_write(second_path, second_content)

def write_or_preview_report(path: Path, content: str, write: bool) -> None:
    if write:
        with workflow_lock():
            _atomic_write(path, content)
        return
    print(f"===== 预览：{relative_repo_path(path)} =====")
    print(content, end="" if content.endswith("\n") else "\n")
