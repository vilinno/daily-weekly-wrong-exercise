"""Anki 无 API 安全 fixture 门禁；只读取临时仓库内 fixture，不生成成品包。"""

from __future__ import annotations

import json
import base64
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_anki import ImageRef, resolve_image  # noqa: E402
from wrong_questions.repo_paths import read_repo_image  # noqa: E402


PNG_FIXTURE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def run_fixture() -> dict[str, object]:
    with TemporaryDirectory(dir=ROOT) as directory:
        fixture_root = Path(directory)
        note = fixture_root / "笔记.md"
        image = fixture_root / "题图.png"
        note.write_text("# fixture\n", encoding="utf-8")
        image.write_bytes(PNG_FIXTURE)

        valid = ImageRef("![[题图.png]]", "题图.png", "题图")
        resolved = resolve_image(valid, note, "fixture", {})
        if resolved != image.resolve():
            raise AssertionError("仓库内 fixture 图片没有被正确解析")
        read_repo_image(resolved)

        for target in ("https://example.test/题图.png", "C:/outside/题图.png", "../outside/题图.png"):
            ref = ImageRef(target, target, "bad")
            if resolve_image(ref, note, "fixture", {}) is not None:
                raise AssertionError(f"外部 Anki 媒体引用未被拒绝：{target}")
    return {"status": "validated", "external_media": "rejected", "fixture": "repo-local"}


def main() -> int:
    try:
        print(json.dumps(run_fixture(), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
