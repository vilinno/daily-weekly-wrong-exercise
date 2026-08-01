"""仓库内文件解析与读取的唯一安全入口。"""

from __future__ import annotations

import ntpath
import os
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

from .foundation import IMAGE_SUFFIXES, ROOT, WorkflowError


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


def _repo_root() -> Path:
    return ROOT.resolve()


def _relative_candidate(raw_ref: str, base_dir: Path | None) -> Path:
    value = unquote(str(raw_ref).strip())
    if not value:
        raise WorkflowError("文件引用不能为空。")
    parsed = urlparse(value)
    if parsed.scheme or value.startswith(("//", "\\\\")):
        raise WorkflowError(f"拒绝非仓库内文件引用：`{raw_ref}`")
    if ntpath.isabs(value) or Path(value).is_absolute():
        raise WorkflowError(f"拒绝绝对路径文件引用：`{raw_ref}`")

    normalized = value.replace("\\", "/")
    base = (base_dir or _repo_root()).resolve()
    try:
        base.relative_to(_repo_root())
    except ValueError as exc:
        raise WorkflowError("文件引用的基准目录不在仓库内。") from exc
    return base / Path(normalized)


def resolve_repo_file(
    raw_ref: str | Path,
    *,
    base_dir: Path | None = None,
    must_exist: bool = True,
    must_be_file: bool = True,
) -> Path:
    """解析仓库内文件，并拒绝路径逃逸、远程 URL 与仓库外符号链接。"""

    if isinstance(raw_ref, Path) and raw_ref.is_absolute():
        candidate = raw_ref
    else:
        candidate = _relative_candidate(str(raw_ref), base_dir)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(_repo_root())
    except ValueError as exc:
        raise WorkflowError(f"文件引用逃逸出仓库：`{raw_ref}`") from exc
    if must_exist and not resolved.exists():
        raise WorkflowError(f"仓库内文件不存在：`{raw_ref}`")
    if must_be_file and resolved.exists() and not resolved.is_file():
        raise WorkflowError(f"仓库内路径不是普通文件：`{raw_ref}`")
    return resolved


def resolve_repo_image(
    raw_ref: str | Path,
    *,
    base_dirs: tuple[Path, ...] = (),
    unique_basename_fallback: bool = False,
) -> Path:
    """在仓库内解析题图；所有候选路径最终都经过 resolve_repo_file。"""

    value = unquote(str(raw_ref).strip())
    if ntpath.isabs(value) or Path(value).is_absolute() or urlparse(value).scheme:
        raise WorkflowError(f"拒绝绝对或远程题图引用：`{raw_ref}`")
    suffix = Path(value.replace("\\", "/")).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise WorkflowError(f"不是允许的图片后缀：`{raw_ref}`")

    candidates: list[tuple[str | Path, Path | None]] = [(value, None)]
    candidates.extend((value, directory) for directory in base_dirs)
    errors: list[str] = []
    for candidate, base_dir in candidates:
        try:
            path = resolve_repo_file(candidate, base_dir=base_dir)
        except WorkflowError as exc:
            errors.append(str(exc))
            continue
        if path.suffix.lower() in IMAGE_SUFFIXES:
            return path

    has_parent_segment = ".." in value.replace("\\", "/").split("/")
    if unique_basename_fallback and not has_parent_segment:
        basename = Path(value.replace("\\", "/")).name.casefold()
        matches = [
            path.resolve()
            for path in _repo_root().rglob(Path(value).name)
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            and path.name.casefold() == basename
        ]
        if len(matches) == 1:
            return resolve_repo_file(matches[0].relative_to(_repo_root()))

    detail = errors[-1] if errors else "未找到唯一匹配项"
    raise WorkflowError(f"题图无法解析：`{raw_ref}`（{detail}）")


def read_repo_image(path: str | Path) -> bytes:
    """安全读取题图，并验证真实图片结构、大小和像素上限。"""

    resolved = resolve_repo_file(path)
    try:
        file_size = resolved.stat().st_size
    except OSError as exc:
        raise WorkflowError(f"图片文件无法读取：`{resolved}`") from exc
    if file_size > MAX_IMAGE_BYTES:
        raise WorkflowError(
            f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB 大小上限：`{resolved}`"
        )
    data = resolved.read_bytes()
    suffix = resolved.suffix.lower()
    valid = (
        (suffix == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (suffix in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff"))
        or (suffix == ".gif" and data.startswith((b"GIF87a", b"GIF89a")))
        or (suffix == ".webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        or (suffix == ".bmp" and data.startswith(b"BM"))
    )
    if not valid:
        raise WorkflowError(f"图片文件头与扩展名不匹配：`{resolved.relative_to(_repo_root()).as_posix()}`")
    try:
        from PIL import Image
        from PIL import ImageFile

        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise WorkflowError(
                    f"图片像素超过 {MAX_IMAGE_PIXELS} 上限：`{resolved}`"
                )
            image.verify()
    except WorkflowError:
        raise
    except ImportError as exc:
        raise WorkflowError("真实图片校验需要安装 Pillow：`python -m pip install -r Anki/requirements.txt`") from exc
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"图片内容无法通过真实格式校验：`{resolved}`（{exc}）") from exc
    return data


def repo_relative(path: str | Path) -> str:
    resolved = resolve_repo_file(path, must_exist=False, must_be_file=False)
    return resolved.relative_to(_repo_root()).as_posix()
