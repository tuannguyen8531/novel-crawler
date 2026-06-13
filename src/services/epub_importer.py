from __future__ import annotations

import html
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urldefrag
from xml.etree import ElementTree

from src.models import ChapterResult, NovelMetadata
from src.services.metadata import metadata_to_dict
from src.utils.text import slugify

CONTAINER_PATH = "META-INF/container.xml"
EPUB_IMAGE_PLACEHOLDER = "[[EPUB_IMAGE:{index}]]"
ILLUSTRATION_MARKER = "[[ILLUSTRATION:{filename}]]"
CHAPTER_PATTERNS = [
    re.compile(r"(?<!\d)(?:제\s*)?(\d+)\s*(?:화|장)(?!\d)", re.IGNORECASE),
    re.compile(r"第\s*(\d+)\s*[章节話话回]", re.IGNORECASE),
    re.compile(r"\b(?:chương|chuong)\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\b(?:chapter|chap\.?|ch\.?)\s*#?\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\b(?:episode|ep\.?)\s*#?\s*(\d+)\b", re.IGNORECASE),
]


@dataclass(frozen=True)
class EpubSection:
    index: int
    source_path: str
    title: str
    text: str
    image_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProcessedChapter:
    number: int
    section: EpubSection


@dataclass(frozen=True)
class EpubBookMetadata:
    title: str | None
    author: str | None


@dataclass(frozen=True)
class EpubBook:
    metadata: EpubBookMetadata
    sections: list[EpubSection]


@dataclass(frozen=True)
class EpubIllustration:
    index: int
    chapter_number: int
    source_path: str
    path: str


@dataclass(frozen=True)
class EpubImportResult:
    metadata: NovelMetadata
    chapters: list[ChapterResult]
    illustrations: list[EpubIllustration]
    output_dir: str
    chapter_output_dir: str
    illustrations_dir: str
    warnings: tuple[str, ...] = ()


class TextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
    SKIP_TAGS = {"head", "script", "style", "svg"}
    TITLE_TAGS = {"h1", "h2", "h3", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._capture_title: str | None = None
        self._title_parts: list[str] = []
        self.image_sources: list[str] = []
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_by_name = {name.casefold(): value for name, value in attrs if value}
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in {"img", "image"}:
            src = (
                attrs_by_name.get("src")
                or attrs_by_name.get("href")
                or attrs_by_name.get("xlink:href")
            )
            if src:
                self.image_sources.append(html.unescape(src))
                self._add_break()
                self._parts.append(
                    EPUB_IMAGE_PLACEHOLDER.format(index=len(self.image_sources))
                )
                self._add_break()
        if tag in self.BLOCK_TAGS:
            self._add_break()
        if not self.title and tag in self.TITLE_TAGS:
            self._capture_title = tag
            self._title_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if self._capture_title == tag:
            self.title = normalize_whitespace(" ".join(self._title_parts))
            self._capture_title = None
            self._title_parts = []
        if tag in self.BLOCK_TAGS:
            self._add_break()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = normalize_inline_markup(html.unescape(data))
        if not text.strip():
            return
        self._parts.append(text)
        if self._capture_title:
            self._title_parts.append(text)

    def get_text(self) -> str:
        text = "".join(self._parts)
        lines = [normalize_whitespace(line) for line in text.splitlines()]
        lines = [line for line in lines if line]
        return "\n\n".join(lines)

    def _add_break(self) -> None:
        if self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")


def import_epub(
    epub_path: Path,
    share_root: Path,
    *,
    name: str | None = None,
    keep_existing: bool = False,
) -> EpubImportResult:
    epub_path = resolve_epub_path(epub_path)
    book = read_epub_book(epub_path)

    fallback_title = name or epub_path.stem
    title = normalize_whitespace(book.metadata.title or fallback_title)
    author = normalize_whitespace(book.metadata.author or "") or None
    source_url = epub_path.resolve().as_uri()
    fallback_slug = slugify(epub_path.stem, fallback="epub")
    novel_slug = slugify(name or epub_path.stem, fallback=fallback_slug)
    processed_chapters = select_processed_chapters(book.sections)
    if not processed_chapters:
        raise EpubImportError(f"no importable chapters found in {epub_path}")

    novel_dir = share_root / novel_slug
    chapter_output_dir = novel_dir / "input"
    illustrations_dir = novel_dir / "illustrations"
    chapter_output_dir.mkdir(parents=True, exist_ok=True)
    illustrations_dir.mkdir(parents=True, exist_ok=True)
    if not keep_existing:
        clean_existing_chapters(chapter_output_dir)
        clean_existing_illustrations(illustrations_dir)

    metadata = NovelMetadata(
        title=title,
        author=author,
        source_url=source_url,
        site_name=novel_slug,
    )
    write_json_atomic(novel_dir / "metadata.json", metadata_to_dict(metadata))

    chapters: list[ChapterResult] = []
    illustrations: list[EpubIllustration] = []
    warnings: list[str] = []
    used_chapters: set[int] = set()
    illustration_index = 0
    with zipfile.ZipFile(epub_path) as epub:
        for chapter in processed_chapters:
            chapter_number = chapter.number
            section = chapter.section
            if chapter_number in used_chapters:
                warnings.append(f"duplicate chapter {chapter_number} skipped: {section.title}")
                continue

            used_chapters.add(chapter_number)
            chapter_text = section.text
            chapter_illustration_index = 0
            for image_index, image_path in enumerate(section.image_paths, start=1):
                try:
                    image_data = epub.read(image_path)
                except KeyError:
                    warnings.append(f"missing image skipped: {image_path}")
                    chapter_text = chapter_text.replace(
                        EPUB_IMAGE_PLACEHOLDER.format(index=image_index),
                        "",
                    )
                    continue

                illustration_index += 1
                chapter_illustration_index += 1
                illustration_output = illustrations_dir / illustration_filename(
                    chapter_number,
                    chapter_illustration_index,
                    image_path,
                )
                write_bytes_atomic(illustration_output, image_data)
                chapter_text = chapter_text.replace(
                    EPUB_IMAGE_PLACEHOLDER.format(index=image_index),
                    ILLUSTRATION_MARKER.format(filename=illustration_output.name),
                )
                illustrations.append(
                    EpubIllustration(
                        index=illustration_index,
                        chapter_number=chapter_number,
                        source_path=image_path,
                        path=str(illustration_output),
                    )
                )

            path = chapter_output_dir / f"chapter_{chapter_number}.txt"
            write_text_atomic(path, chapter_text.strip() + "\n")
            chapters.append(
                ChapterResult(
                    index=chapter_number,
                    title=section.title,
                    source_url=f"{source_url}#{section.source_path}",
                    path=str(path),
                )
            )

    chapters.sort(key=lambda chapter_result: chapter_result.index)
    return EpubImportResult(
        metadata=metadata,
        chapters=chapters,
        illustrations=illustrations,
        output_dir=str(novel_dir),
        chapter_output_dir=str(chapter_output_dir),
        illustrations_dir=str(illustrations_dir),
        warnings=tuple(warnings),
    )


def resolve_epub_path(epub_path: Path) -> Path:
    if epub_path.suffix.lower() != ".epub":
        raise EpubImportError(f"{epub_path} is not an .epub file")
    if not epub_path.is_file():
        raise EpubImportError(f"EPUB file not found: {epub_path}")
    return epub_path


def read_epub_book(epub_path: Path) -> EpubBook:
    try:
        with zipfile.ZipFile(epub_path) as epub:
            opf_path = get_opf_path(epub)
            metadata = read_epub_metadata(epub, opf_path)
            section_paths = get_spine_document_paths(epub, opf_path)
            sections = []
            for path in section_paths:
                section = read_section(epub, path, len(sections) + 1)
                if section.text or section.image_paths:
                    sections.append(section)
    except zipfile.BadZipFile as exc:
        raise EpubImportError(f"invalid EPUB zip: {epub_path}") from exc
    except KeyError as exc:
        raise EpubImportError(f"missing EPUB member: {exc}") from exc
    except ElementTree.ParseError as exc:
        raise EpubImportError(f"invalid EPUB XML: {exc}") from exc

    if not sections:
        raise EpubImportError(f"no readable text sections found in {epub_path}")
    return EpubBook(metadata=metadata, sections=sections)


def get_opf_path(epub: zipfile.ZipFile) -> str:
    root = ElementTree.fromstring(epub.read(CONTAINER_PATH))
    rootfile = root.find(".//{*}rootfile")
    if rootfile is None:
        raise EpubImportError(f"{CONTAINER_PATH} does not declare an OPF rootfile")
    opf_path = rootfile.attrib.get("full-path")
    if not opf_path:
        raise EpubImportError(f"{CONTAINER_PATH} rootfile is missing full-path")
    return opf_path


def read_epub_metadata(epub: zipfile.ZipFile, opf_path: str) -> EpubBookMetadata:
    opf_root = ElementTree.fromstring(epub.read(opf_path))
    metadata_node = opf_root.find(".//{*}metadata")
    if metadata_node is None:
        return EpubBookMetadata(title=None, author=None)
    return EpubBookMetadata(
        title=find_child_text(metadata_node, "title"),
        author=find_child_text(metadata_node, "creator"),
    )


def find_child_text(element: ElementTree.Element, local_name: str) -> str | None:
    for child in element.iter():
        if child is element:
            continue
        if xml_local_name(child.tag) != local_name:
            continue
        text = normalize_whitespace(child.text or "")
        if text:
            return text
    return None


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def get_spine_document_paths(epub: zipfile.ZipFile, opf_path: str) -> list[str]:
    opf_root = ElementTree.fromstring(epub.read(opf_path))
    manifest = {
        item.attrib["id"]: item.attrib["href"]
        for item in opf_root.findall(".//{*}manifest/{*}item")
        if "id" in item.attrib and "href" in item.attrib
    }

    opf_dir = posixpath.dirname(opf_path)
    paths: list[str] = []
    for itemref in opf_root.findall(".//{*}spine/{*}itemref"):
        idref = itemref.attrib.get("idref")
        href = manifest.get(idref or "")
        if not href:
            continue
        clean_href = unquote(urldefrag(href)[0])
        normalized_path = posixpath.normpath(posixpath.join(opf_dir, clean_href))
        if normalized_path in epub.namelist():
            paths.append(normalized_path)

    if not paths:
        raise EpubImportError("EPUB spine does not contain readable document paths")
    return paths


def read_section(epub: zipfile.ZipFile, source_path: str, index: int) -> EpubSection:
    raw = epub.read(source_path)
    markup = decode_bytes(raw)
    extractor = TextExtractor()
    extractor.feed(markup)
    extractor.close()
    text = extractor.get_text()
    title = extractor.title or Path(source_path).stem
    image_paths = []
    for source_index, image_source in enumerate(extractor.image_sources, start=1):
        source_placeholder = EPUB_IMAGE_PLACEHOLDER.format(index=source_index)
        resolved_path = resolve_resource_path(source_path, image_source)
        if not resolved_path or resolved_path not in epub.namelist():
            text = text.replace(source_placeholder, "")
            continue
        image_paths.append(resolved_path)
        target_placeholder = EPUB_IMAGE_PLACEHOLDER.format(index=len(image_paths))
        text = text.replace(source_placeholder, target_placeholder)
    return EpubSection(
        index=index,
        source_path=source_path,
        title=title,
        text=text,
        image_paths=tuple(image_paths),
    )


def resolve_resource_path(section_path: str, resource_ref: str) -> str | None:
    clean_ref = unquote(urldefrag(resource_ref)[0]).strip()
    if not clean_ref:
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*:", clean_ref, re.IGNORECASE):
        return None
    if clean_ref.startswith("/"):
        return posixpath.normpath(clean_ref.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(section_path), clean_ref))


def select_processed_chapters(sections: list[EpubSection]) -> list[ProcessedChapter]:
    explicit_chapters = []
    for section in sections:
        if should_skip_fallback_section(section):
            continue
        chapter_number = detect_chapter_number(section.title)
        if chapter_number is not None:
            explicit_chapters.append(ProcessedChapter(chapter_number, section))

    if explicit_chapters:
        return explicit_chapters

    fallback_chapters = []
    for section in sections:
        if should_skip_fallback_section(section):
            continue
        fallback_chapters.append(ProcessedChapter(len(fallback_chapters) + 1, section))
    return fallback_chapters


def detect_chapter_number(title: str) -> int | None:
    normalized_title = normalize_whitespace(title)
    for pattern in CHAPTER_PATTERNS:
        match = pattern.search(normalized_title)
        if match:
            return int(match.group(1))
    return None


def should_skip_fallback_section(section: EpubSection) -> bool:
    title = normalize_whitespace(section.title)
    title_lower = title.casefold()
    source_name = Path(section.source_path).name.casefold()
    metadata_markers = ("author:", "tags:", "status:", "synopsis")
    front_matter_names = (
        "cover",
        "copyright",
        "contents",
        "nav",
        "navigation",
        "titlepage",
        "title-page",
        "toc",
    )
    notice_markers = ("notice", "공지")

    if not title:
        return True
    if any(marker in title_lower for marker in notice_markers):
        return True
    if any(name in title_lower or name in source_name for name in front_matter_names):
        return True
    return section.index <= 5 and any(
        marker in section.text.casefold() for marker in metadata_markers
    )


def clean_existing_chapters(chapter_output_dir: Path) -> None:
    for existing_file in chapter_output_dir.glob("chapter_*.txt"):
        existing_file.unlink()


def clean_existing_illustrations(illustrations_dir: Path) -> None:
    for existing_file in illustrations_dir.glob("*-*.*"):
        if existing_file.is_file():
            existing_file.unlink()


def illustration_filename(chapter_number: int, chapter_image_number: int, source_path: str) -> str:
    suffix = Path(source_path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}:
        suffix = ".img"
    return f"{chapter_number:03d}-{chapter_image_number:03d}{suffix}"


def write_json_atomic(path: Path, data: object) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def write_text_atomic(path: Path, text: str) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_bytes(data)
    temp_path.replace(path)


def decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def normalize_whitespace(value: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def normalize_inline_markup(value: str) -> str:
    return re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.IGNORECASE)


class EpubImportError(Exception):
    pass
