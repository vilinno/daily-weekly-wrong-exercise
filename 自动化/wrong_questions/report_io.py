"""报告写入与只读预览。"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import os
from pathlib import Path
import socket
import tempfile
import uuid
from functools import wraps
from inspect import signature
from typing import Any, Callable, Iterator, TypeVar

from .git_store import relative_repo_path
from .foundation import ROOT, WorkflowError


LOCK_PATH = ROOT / "报告" / ".workflow.lock"
LOCK_MAX_AGE = dt.timedelta(hours=6)
_LOCK_HELD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "wrong_questions_workflow_lock_held", default=False
)
Function = TypeVar("Function", bound=Callable[..., Any])


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _stale_lock() -> bool:
    try:
        metadata = LOCK_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    values = dict(
        line.split("=", 1)
        for line in metadata.splitlines()
        if "=" in line
        for line in [line.strip()]
    )
    try:
        pid = int(values.get("pid", "0"))
    except ValueError:
        pid = 0
    if values.get("host") == socket.gethostname() and pid and not _pid_is_alive(pid):
        return True
    started_at = values.get("started_at", "")
    try:
        started = dt.datetime.fromisoformat(started_at)
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc) - started.astimezone(dt.timezone.utc) > LOCK_MAX_AGE


@contextlib.contextmanager
def workflow_lock() -> Iterator[None]:
    """以仓库内独占锁保护同一时间的报告/答案生成。"""

    if _LOCK_HELD.get():
        yield
        return
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        if _stale_lock():
            stale_path = LOCK_PATH.with_name(
                f"{LOCK_PATH.name}.stale-{uuid.uuid4().hex[:8]}"
            )
            try:
                os.replace(LOCK_PATH, stale_path)
                stale_path.unlink(missing_ok=True)
                handle = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, OSError) as stale_exc:
                raise WorkflowError(
                    f"检测到陈旧自动化锁，但无法安全接管：{relative_repo_path(LOCK_PATH)}"
                ) from stale_exc
        else:
            raise WorkflowError(
                f"已有自动化运行正在写入报告：{relative_repo_path(LOCK_PATH)}"
            ) from exc
    token = _LOCK_HELD.set(True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(f"pid={os.getpid()}\n")
            stream.write(f"host={socket.gethostname()}\n")
            stream.write(f"started_at={dt.datetime.now(dt.timezone.utc).isoformat()}\n")
            stream.write(f"run_id={uuid.uuid4().hex}\n")
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        _LOCK_HELD.reset(token)
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass


def workflow_entry(function: Function) -> Function:
    """让公开写模式工作流从收集阶段起就持有仓库锁。"""

    function_signature = signature(function)

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        bound = function_signature.bind_partial(*args, **kwargs)
        write = bool(bound.arguments.get("write", True))
        if write:
            with workflow_lock():
                return function(*args, **kwargs)
        return function(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


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


def _temporary_bytes(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        return stream.name


def _restore_bytes(path: Path, data: bytes | None) -> None:
    if data is None:
        path.unlink(missing_ok=True)
        return
    temporary_name = _temporary_bytes(path, data)
    try:
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


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
        first_old = first_path.read_bytes() if first_path.exists() else None
        second_old = second_path.read_bytes() if second_path.exists() else None
        first_temp: str | None = None
        second_temp: str | None = None
        try:
            first_temp = _temporary_bytes(first_path, first_content.encode("utf-8"))
            second_temp = _temporary_bytes(second_path, second_content.encode("utf-8"))
            os.replace(first_temp, first_path)
            first_temp = None
            os.replace(second_temp, second_path)
            second_temp = None
        except Exception:
            _restore_bytes(first_path, first_old)
            _restore_bytes(second_path, second_old)
            raise
        finally:
            if first_temp:
                Path(first_temp).unlink(missing_ok=True)
            if second_temp:
                Path(second_temp).unlink(missing_ok=True)

def write_or_preview_report(path: Path, content: str, write: bool) -> None:
    if write:
        with workflow_lock():
            _atomic_write(path, content)
        return
    print(f"===== 预览：{relative_repo_path(path)} =====")
    print(content, end="" if content.endswith("\n") else "\n")
