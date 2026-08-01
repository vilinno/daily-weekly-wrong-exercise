from __future__ import annotations

import argparse
import base64
import copy
import csv
import hashlib
import html
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import markdown
from anki.collection import (
    Collection,
    DeckIdLimit,
    ExportAnkiPackageOptions,
    ImportAnkiPackageRequest,
)


ROOT = Path(__file__).resolve().parents[1]
AUTOMATION_DIR = ROOT / "自动化"
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))
from wrong_questions.foundation import WorkflowError  # noqa: E402
from wrong_questions.repo_paths import read_repo_image, resolve_repo_file, resolve_repo_image  # noqa: E402
from wrong_questions.question_index import load_question_index, question_id_for_image  # noqa: E402
OUTPUT_DIR = ROOT / "Anki"
PACKAGE_PATH = OUTPUT_DIR / "每日错题-408与数学.apkg"
MANIFEST_PATH = OUTPUT_DIR / "卡片清单.csv"
ISSUES_PATH = OUTPUT_DIR / "待补清单.md"
REPORT_PATH = OUTPUT_DIR / "构建报告.md"

NOTE_TYPE_ID = 1_776_010_731_001
FIELD_IDS = [1_776_010_731_101, 1_776_010_731_102, 1_776_010_731_103, 1_776_010_731_104]
TEMPLATE_ID = 1_776_010_731_201
PARENT_DECK = "每日错题"

ANSWER_TITLES = ("解答", "总结", "讲解", "解析", "答案", "辨析图示")
PLACEHOLDER_RE = re.compile(r"^\s*(?:todo|待补|待填写|待复盘|待确认)\s*[。.!！]?$", re.I)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
QUESTION_HEADING_RE = re.compile(r"^题目(?:[一二三四五六七八九十0-9]+)?(?:\s|$|[-—–（(:：])")
WIKI_IMAGE_RE = re.compile(r"!\[\[([^\]]+)\]\]")
MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def subject_specs() -> list[dict[str, object]]:
    """从配置读取科目和 Anki 参数，不按科目名称写死构建逻辑。"""

    config = json.loads((ROOT / "自动化" / "config.json").read_text(encoding="utf-8"))
    subjects = config.get("subjects", [])
    if isinstance(subjects, dict):
        subjects = [{"name": name, "path": path} for name, path in subjects.items()]
    specs: list[dict[str, object]] = []
    for item in subjects:
        anki = dict(item.get("anki", {})) if isinstance(item, dict) else {}
        name = str(item["name"])
        specs.append(
            {
                "name": name,
                "path": str(item["path"]),
                "deck": str(anki.get("deck", f"{PARENT_DECK}::{name}")),
                "preset": str(anki.get("preset", f"{name}错题")),
                "desired_retention": float(anki.get("desired_retention", 0.9)),
                "new_per_day": int(anki.get("new_per_day", 10)),
                "new_delays": list(anki.get("new_delays", [10.0])),
                "new_intervals": list(anki.get("new_intervals", [3, 10, 0])),
                "review_modifier": float(anki.get("review_modifier", 1.0)),
                "max_interval": int(anki.get("max_interval", 36500)),
                "lapse_delays": list(anki.get("lapse_delays", [30.0])),
            }
        )
    return specs


@dataclass
class Heading:
    level: int
    text: str
    line: int


@dataclass
class ImageRef:
    raw: str
    target: str
    alt: str
    resolved: Path | None = None
    media_name: str | None = None


@dataclass
class CardCandidate:
    group: str
    source: Path
    line: int
    title: str
    context: list[str]
    question_markdown: str
    answer_markdown: str
    question_images: list[ImageRef] = field(default_factory=list)
    answer_images: list[ImageRef] = field(default_factory=list)
    status: str = "待检查"
    reason: str = ""
    question_id: str | None = None


def clean_heading(text: str) -> str:
    text = re.sub(r"\s*#+\s*$", "", text).strip()
    text = re.sub(r"^题目\s*[-—–：:]?\s*", "", text).strip()
    return text


def is_question_heading(text: str) -> bool:
    return bool(QUESTION_HEADING_RE.match(text.strip()))


def note_files() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    config = json.loads((ROOT / "自动化" / "config.json").read_text(encoding="utf-8"))
    subjects = config.get("subjects", [])
    if isinstance(subjects, dict):
        subjects = [{"name": name, "path": path} for name, path in subjects.items()]
    for item in subjects:
        group, configured_path = str(item["name"]), str(item["path"])
        subject_root = resolve_repo_file(configured_path, must_exist=True, must_be_file=False)
        for path in sorted(subject_root.rglob("*.md")):
            found.append((group, path))
    return found


def parse_candidates(group: str, path: Path) -> list[CardCandidate]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    headings: list[Heading] = []
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match:
            headings.append(Heading(len(match.group(1)), match.group(2).strip(), index + 1))

    candidates: list[CardCandidate] = []
    for heading_index, heading in enumerate(headings):
        if not is_question_heading(heading.text):
            continue

        start = heading.line
        boundary = len(lines) + 1
        for later in headings[heading_index + 1 :]:
            if is_question_heading(later.text) or later.level < heading.level:
                boundary = later.line
                break

        answer_heading: Heading | None = None
        for later in headings[heading_index + 1 :]:
            if later.line >= boundary:
                break
            if later.level <= heading.level and any(key in later.text for key in ANSWER_TITLES):
                answer_heading = later
                break

        question_end = answer_heading.line if answer_heading else boundary
        question_markdown = "\n".join(lines[start: question_end - 1]).strip()
        answer_markdown = ""
        if answer_heading:
            answer_markdown = "\n".join(lines[answer_heading.line - 1 : boundary - 1]).strip()

        prior = [h for h in headings if h.line < heading.line and h.level < heading.level]
        context_by_level: dict[int, str] = {}
        for item in prior:
            context_by_level[item.level] = item.text
            for level in list(context_by_level):
                if level > item.level:
                    del context_by_level[level]
        context = [context_by_level[level] for level in sorted(context_by_level)]

        own_title = clean_heading(heading.text)
        title = own_title or (context[-1] if context else path.stem)
        candidates.append(
            CardCandidate(
                group=group,
                source=path,
                line=heading.line,
                title=title,
                context=context,
                question_markdown=question_markdown,
                answer_markdown=answer_markdown,
            )
        )
    return candidates


def extract_images(markdown_text: str) -> list[ImageRef]:
    refs: list[ImageRef] = []
    for match in WIKI_IMAGE_RE.finditer(markdown_text):
        target = match.group(1).split("|", 1)[0].strip()
        refs.append(ImageRef(match.group(0), target, Path(target).stem))
    for match in MD_IMAGE_RE.finditer(markdown_text):
        target = match.group(2).strip().strip("<>")
        target = target.split(" ", 1)[0].strip('"\'')
        refs.append(ImageRef(match.group(0), target, match.group(1).strip() or Path(target).stem))
    return refs


def image_index() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    config = json.loads((ROOT / "自动化" / "config.json").read_text(encoding="utf-8"))
    subjects = config.get("subjects", [])
    if isinstance(subjects, dict):
        subjects = [{"name": name, "path": path} for name, path in subjects.items()]
    for item in subjects:
        subject_root = resolve_repo_file(str(item["path"]), must_exist=True, must_be_file=False)
        for path in subject_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                index.setdefault(path.name.casefold(), []).append(path.resolve())
    return index


def resolve_image(ref: ImageRef, source: Path, group: str, index: dict[str, list[Path]]) -> Path | None:
    try:
        return resolve_repo_image(
            ref.target,
            base_dirs=(
                source.parent,
                source.parent / "assets",
                source.parent.parent / "assets",
                ROOT / group / "assets",
            ),
            unique_basename_fallback=True,
        )
    except WorkflowError:
        return None


def meaningful_answer(markdown_text: str) -> bool:
    without_headings = re.sub(r"^#{1,6}\s+.*$", "", markdown_text, flags=re.M)
    without_images = WIKI_IMAGE_RE.sub("", MD_IMAGE_RE.sub("", without_headings))
    plain = re.sub(r"[`*_#>\-=\s]", "", without_images)
    return bool(plain) and not PLACEHOLDER_RE.fullmatch(plain)


def inspect_candidates(candidates: list[CardCandidate]) -> None:
    index = image_index()
    for card in candidates:
        card.question_images = extract_images(card.question_markdown)
        card.answer_images = extract_images(card.answer_markdown)
        for ref in card.question_images + card.answer_images:
            ref.resolved = resolve_image(ref, card.source, card.group, index)
        if card.question_images and card.question_images[0].resolved:
            try:
                card.question_id = question_id_for_image(
                    card.question_images[0].resolved, load_question_index()
                )
            except WorkflowError as exc:
                card.status, card.reason = "未导入", f"题目 ID 无法确定：{exc}"
                continue

        if not card.question_images:
            card.status, card.reason = "未导入", "题目区域没有题图"
        elif any(ref.resolved is None for ref in card.question_images):
            missing = "、".join(ref.target for ref in card.question_images if ref.resolved is None)
            card.status, card.reason = "未导入", f"题图无法定位：{missing}"
        elif not card.answer_markdown:
            card.status, card.reason = "未导入", "没有紧随题目的解答、总结、讲解或解析"
        elif not meaningful_answer(card.answer_markdown):
            card.status, card.reason = "未导入", "答案区域为空或仍是占位内容"
        elif any(ref.resolved is None for ref in card.answer_images):
            missing = "、".join(ref.target for ref in card.answer_images if ref.resolved is None)
            card.status, card.reason = "未导入", f"解析中的图片无法定位：{missing}"
        else:
            card.status, card.reason = "已导入", ""


def stable_media_name(path: Path, used: dict[str, Path]) -> str:
    basename = path.name
    folded = basename.casefold()
    if folded not in used or used[folded] == path:
        used[folded] = path
        return basename
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    renamed = f"{path.stem}-{digest}{path.suffix.lower()}"
    used[renamed.casefold()] = path
    return renamed


def convert_math(text: str) -> str:
    placeholders: dict[str, str] = {}

    def hold(value: str) -> str:
        key = f"ANKIMATH{len(placeholders)}TOKEN"
        placeholders[key] = value
        return key

    text = re.sub(r"\$\$(.+?)\$\$", lambda m: hold(r"\[" + m.group(1).strip() + r"\]"), text, flags=re.S)
    text = re.sub(
        r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$",
        lambda m: hold(r"\(" + m.group(1).strip() + r"\)"),
        text,
        flags=re.S,
    )
    text = re.sub(r"==(.+?)==", r"<mark>\1</mark>", text, flags=re.S)
    for key, value in placeholders.items():
        text = text.replace(key, value)
    return text


def markdown_to_html(markdown_text: str, refs: list[ImageRef]) -> str:
    converted = markdown_text
    for index, ref in enumerate(refs):
        if not ref.media_name:
            continue
        token = f"ANKIIMAGETOKEN{index}"
        converted = converted.replace(ref.raw, token, 1)
        image_html = (
            f'<div class="note-image"><img src="{html.escape(ref.media_name, quote=True)}" '
            f'alt="{html.escape(ref.alt, quote=True)}"></div>'
        )
        converted = converted.replace(token, image_html, 1)
    converted = convert_math(converted)
    return markdown.markdown(
        converted,
        extensions=["extra", "sane_lists"],
        output_format="html5",
    )


def stable_guid(card: CardCandidate) -> str:
    key = card.question_id or f"{card.source.relative_to(ROOT).as_posix()}:{card.line}"
    digest = hashlib.sha1(key.encode("utf-8")).digest()[:10]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_notetype(col: Collection):
    model = col.models.new("每日错题（题图→答案与解析）")
    for field_name, field_id in zip(["题目ID", "题目", "答案与解析", "来源"], FIELD_IDS, strict=True):
        field = col.models.new_field(field_name)
        field["id"] = field_id
        col.models.add_field(model, field)
    template = col.models.new_template("题图问答")
    template["id"] = TEMPLATE_ID
    template["qfmt"] = "{{题目}}"
    template["afmt"] = "{{FrontSide}}<hr id=answer><section class=answer>{{答案与解析}}</section><footer>Question ID：{{题目ID}}<br>{{来源}}</footer>"
    col.models.add_template(model, template)
    model["css"] = """
.card {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  font-size: 18px;
  line-height: 1.65;
  color: #202124;
  background: #fafafa;
  text-align: left;
  max-width: 920px;
  margin: 0 auto;
  padding: 18px;
}
.deck-label { color: #5f6368; font-size: 13px; margin-bottom: 6px; }
.card-title { font-size: 21px; font-weight: 700; margin: 0 0 14px; }
.note-image { text-align: center; margin: 12px 0; }
.note-image img { max-width: 100%; height: auto; border-radius: 8px; }
.answer { background: #fff; border-radius: 10px; padding: 8px 18px; }
.answer h4, .answer h5 { margin: 12px 0 6px; }
.answer p { margin: 8px 0; }
.answer li { margin: 4px 0; }
mark { background: #fff1a8; padding: 0 .12em; }
footer { color: #777; font-size: 12px; margin-top: 14px; }
.nightMode .card { color: #e8eaed; background: #202124; }
.nightMode .answer { background: #292a2d; }
.nightMode mark { color: #202124; }
"""
    changes = col.models.add(model)
    return col.models.get(changes.id)


def assign_stable_notetype_id(collection_path: Path, generated_id: int) -> None:
    if generated_id == NOTE_TYPE_ID:
        return
    connection = sqlite3.connect(collection_path)
    try:
        connection.create_collation("unicase", lambda left, right: (left.casefold() > right.casefold()) - (left.casefold() < right.casefold()))
        if connection.execute("SELECT 1 FROM notetypes WHERE id = ?", (NOTE_TYPE_ID,)).fetchone():
            raise RuntimeError(f"固定笔记类型 ID 已存在：{NOTE_TYPE_ID}")
        connection.execute("UPDATE notetypes SET id = ? WHERE id = ?", (NOTE_TYPE_ID, generated_id))
        connection.execute("UPDATE fields SET ntid = ? WHERE ntid = ?", (NOTE_TYPE_ID, generated_id))
        connection.execute("UPDATE templates SET ntid = ? WHERE ntid = ?", (NOTE_TYPE_ID, generated_id))
        connection.execute("UPDATE notes SET mid = ? WHERE mid = ?", (NOTE_TYPE_ID, generated_id))
        connection.commit()
    finally:
        connection.close()


def configure_decks(col: Collection) -> tuple[int, dict[str, int]]:
    parent_id = int(col.decks.id(PARENT_DECK))
    specs = subject_specs()
    deck_names = {str(spec["name"]): str(spec["deck"]) for spec in specs}
    deck_ids = {group: int(col.decks.id(name)) for group, name in deck_names.items()}
    default = col.decks.get_config(1)

    for spec in specs:
        group = str(spec["name"])
        config = copy.deepcopy(default)
        config_id = col.decks.add_config_returning_id(str(spec["preset"]), config)
        config = col.decks.get_config(config_id)
        config["desiredRetention"] = float(spec["desired_retention"])
        config["new"].update(
            {
                "delays": list(spec["new_delays"]),
                "ints": list(spec["new_intervals"]),
                "perDay": int(spec["new_per_day"]),
            }
        )
        config["rev"].update(
            {
                "ivlFct": float(spec["review_modifier"]),
                "maxIvl": int(spec["max_interval"]),
                "perDay": 9999,
            }
        )
        config["lapse"].update({"delays": list(spec["lapse_delays"]), "minInt": 1})
        col.decks.update_config(config)
        deck = col.decks.get(deck_ids[group])
        col.decks.set_config_id_for_deck_dict(deck, config_id)
    return parent_id, deck_ids


def source_label(card: CardCandidate) -> str:
    relative = card.source.relative_to(ROOT).as_posix()
    return f"Question ID：{card.question_id or '待生成'}；来源：{relative}（第 {card.line} 行）"


def build_package(cards: list[CardCandidate]) -> dict[str, int]:
    included = [card for card in cards if card.status == "已导入"]
    media_paths = sorted(
        {
            ref.resolved
            for card in included
            for ref in card.question_images + card.answer_images
            if ref.resolved is not None
        },
        key=lambda path: str(path),
    )
    media_names: dict[Path, str] = {}
    used_names: dict[str, Path] = {}
    for path in media_paths:
        media_names[path] = stable_media_name(path, used_names)
    for card in included:
        for ref in card.question_images + card.answer_images:
            if ref.resolved:
                ref.media_name = media_names[ref.resolved]

    temp_dir = Path(tempfile.mkdtemp(prefix="anki-build-"))
    collection_path = temp_dir / "collection.anki2"
    try:
        col = Collection(str(collection_path))
        model = make_notetype(col)
        parent_id, deck_ids = configure_decks(col)
        generated_model_id = int(model["id"])
        col.close()
        assign_stable_notetype_id(collection_path, generated_model_id)
        col = Collection(str(collection_path))
        model = col.models.get(NOTE_TYPE_ID)

        actual_media_names: dict[Path, str] = {}
        for path in media_paths:
            image_bytes = read_repo_image(path)
            desired = media_names[path]
            if path.name == desired:
                actual = col.media.write_data(desired, image_bytes)
            else:
                actual = col.media.write_data(desired, image_bytes)
            actual_media_names[path] = actual
        for card in included:
            for ref in card.question_images + card.answer_images:
                if ref.resolved:
                    ref.media_name = actual_media_names[ref.resolved]

        counts = {str(spec["name"]): 0 for spec in subject_specs()}
        for card in included:
            breadcrumbs = " · ".join([card.group, card.source.stem, *card.context])
            question_body = markdown_to_html(card.question_markdown, card.question_images)
            front = (
                f'<div class="deck-label">{html.escape(breadcrumbs)}</div>'
                f'<div class="card-title">{html.escape(card.title)}</div>'
                f"{question_body}"
            )
            answer = markdown_to_html(card.answer_markdown, card.answer_images)
            note = col.new_note(model)
            note.guid = stable_guid(card)
            note["题目ID"] = card.question_id or ""
            note["题目"] = front
            note["答案与解析"] = answer
            note["来源"] = html.escape(source_label(card))
            note.tags = [
                "每日错题",
                card.group,
                f"qid::{card.question_id}",
                *[part for part in card.source.stem.split("-") if part],
            ]
            col.add_note(note, deck_ids[card.group])
            counts[card.group] += 1

        if PACKAGE_PATH.exists():
            PACKAGE_PATH.unlink()
        options = ExportAnkiPackageOptions(
            with_scheduling=False,
            with_deck_configs=True,
            with_media=True,
            legacy=True,
        )
        col.export_anki_package(
            out_path=str(PACKAGE_PATH),
            options=options,
            limit=DeckIdLimit(parent_id),
        )
        col.close()
        counts["媒体"] = len(media_paths)
        return counts
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def verify_package(expected: dict[str, int]) -> dict[str, int]:
    temp_dir = Path(tempfile.mkdtemp(prefix="anki-verify-"))
    try:
        ascii_package = temp_dir / "deck.apkg"
        shutil.copy2(PACKAGE_PATH, ascii_package)
        col = Collection(str(temp_dir / "collection.anki2"))
        request = ImportAnkiPackageRequest(package_path=str(ascii_package))
        request.options.with_deck_configs = True
        request.options.with_scheduling = False
        col.import_anki_package(request)

        note_ids = col.find_notes("")
        card_ids = col.find_cards("")
        if len(note_ids) != sum(value for key, value in expected.items() if key != "媒体"):
            raise RuntimeError(f"导入后的笔记数量异常：{len(note_ids)}")
        if len(card_ids) != len(note_ids):
            raise RuntimeError(f"导入后的卡片数量异常：{len(card_ids)}")

        media_refs: set[str] = set()
        for note_id in note_ids:
            note = col.get_note(note_id)
            if not note["题目ID"] or not any(tag.startswith("qid::") for tag in note.tags):
                raise RuntimeError(f"卡片 {note_id} 缺少独立 Question ID 字段或标签")
            if "<img " not in note["题目"]:
                raise RuntimeError(f"卡片 {note_id} 的问题面没有题图")
            answer_text = re.sub(r"<[^>]+>", "", note["答案与解析"]).strip()
            if not answer_text:
                raise RuntimeError(f"卡片 {note_id} 的答案面为空")
            media_refs.update(col.media.files_in_str(note.mid, note["题目"] + note["答案与解析"]))
        missing_media = [name for name in media_refs if not (Path(col.media.dir()) / name).is_file()]
        if missing_media:
            raise RuntimeError(f"导入后缺少媒体：{'、'.join(sorted(missing_media))}")

        configs = {config["name"]: config for config in col.decks.all_config()}
        decks = {deck["name"]: deck for deck in col.decks.all()}
        for spec in subject_specs():
            group = str(spec["name"])
            preset_name = str(spec["preset"])
            retention = float(spec["desired_retention"])
            config = configs.get(preset_name)
            deck = decks.get(str(spec["deck"]))
            if config is None or deck is None:
                raise RuntimeError(f"导入后缺少 {group} 卡组或复习预设")
            if deck.get("conf") != config.get("id"):
                raise RuntimeError(f"{group} 卡组没有绑定预期复习预设")
            if abs(float(config.get("desiredRetention", 0)) - retention) > 1e-9:
                raise RuntimeError(f"{group} 卡组的目标保持率不正确")
            count = len(col.find_cards(f'deck:"{spec["deck"]}"'))
            if count != expected[group]:
                raise RuntimeError(f"{group} 卡组导入后为 {count} 张，预期 {expected[group]} 张")

        result = {"笔记": len(note_ids), "卡片": len(card_ids), "媒体引用": len(media_refs)}
        col.close()
        return result
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def write_manifest(cards: list[CardCandidate]) -> None:
    with MANIFEST_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["卡组", "状态", "Question ID", "标题", "来源文件", "行号", "题图", "未导入原因"])
        for card in cards:
            writer.writerow(
                [
                    card.group,
                    card.status,
                    card.question_id or "",
                    card.title,
                    card.source.relative_to(ROOT).as_posix(),
                    card.line,
                    "；".join(ref.target for ref in card.question_images),
                    card.reason,
                ]
            )


def write_issues(cards: list[CardCandidate]) -> None:
    excluded = [card for card in cards if card.status != "已导入"]
    lines = [
        "# Anki 待补清单",
        "",
        "以下条目没有进入成品卡组。构建器不会补写答案，也不会修改原笔记。",
        "",
    ]
    if not excluded:
        lines.append("当前没有待补条目。")
    else:
        for card in excluded:
            source = card.source.relative_to(ROOT).as_posix()
            lines.extend(
                [
                    f"- **{card.group}｜{card.title}**",
                    f"  - 位置：`{source}:{card.line}`",
                    f"  - 原因：{card.reason}",
                ]
            )
    ISSUES_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(cards: list[CardCandidate], counts: dict[str, int], verification: dict[str, int]) -> None:
    excluded = sum(card.status != "已导入" for card in cards)
    lines = [
        "# Anki 构建报告",
        "",
        "## 生成结果",
        "",
        f"- 导入包：`{PACKAGE_PATH.name}`",
        *[f"- {spec['name']} 卡片：{counts[str(spec['name'])]} 张" for spec in subject_specs()],
        f"- 媒体文件：{counts['媒体']} 个",
        f"- 未导入条目：{excluded} 条",
        f"- 全新资料库导入验证：通过（{verification['卡片']} 张卡片，{verification['媒体引用']} 个媒体引用均可用）",
        "",
        "## 复习预设",
        "",
        "| 卡组 | 用法 | FSRS 目标保持率 | 新卡/日 | 学习步长 | 旧调度器间隔修正 |",
        "|---|---|---:|---:|---|---:|",
        *[
            f"| {spec['name']} | 配置驱动的错题复习 | {float(spec['desired_retention']):.0%} | {spec['new_per_day']} | {', '.join(str(value) for value in spec['new_delays'])} | {float(spec['review_modifier']):.0%} |"
            for spec in subject_specs()
        ],
        "",
        "两套预设均已写入 `.apkg`。FSRS 是全局开关：如果你的 Anki 已启用 FSRS，会使用目标保持率；如果没有启用，会使用旧调度器中的学习步长、毕业间隔和间隔修正。",
        "",
        "## 数据规则",
        "",
        "- 问题面包含题图，并显示笔记标题与知识点上下文。",
        "- 答案面只使用原笔记中紧随题目的“解答 / 总结 / 讲解 / 解析”，不补写答案。",
        "- 空白、`todo`、缺图或图片无法定位的条目不会导入，详见《待补清单》。",
        "- 每张卡保留来源 Markdown 路径和行号，便于回到 Obsidian 核对。",
    ]
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="从每日错题 Obsidian 笔记生成 Anki 卡组")
    parser.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cards = [card for group, path in note_files() for card in parse_candidates(group, path)]
    inspect_candidates(cards)
    counts = build_package(cards)
    verification = verify_package(counts)
    write_manifest(cards)
    write_issues(cards)
    write_report(cards, counts, verification)
    print(
        json.dumps(
            {"卡片": counts, "未导入": sum(c.status != "已导入" for c in cards), "验证": verification},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
