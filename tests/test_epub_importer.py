from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from src.services.epub_importer import (
    EpubSection,
    TextExtractor,
    detect_chapter_number,
    import_epub,
    select_processed_chapters,
)


class EpubImporterTest(unittest.TestCase):
    def test_text_extractor_keeps_image_position_between_paragraphs(self) -> None:
        extractor = TextExtractor()
        extractor.feed('<p>Before image.</p><img src="image.jpg"/><p>After image.</p>')
        extractor.close()

        self.assertEqual(
            extractor.get_text(),
            "Before image.\n\n[[EPUB_IMAGE:1]]\n\nAfter image.",
        )

    def test_detects_supported_chapter_formats(self) -> None:
        cases = {
            "1화 - 회귀": 1,
            "제12화 재회": 12,
            "3장 시작": 3,
            "Chương 4: Khởi đầu": 4,
            "Chuong 5 - Gap lai": 5,
            "Chapter 6: Return": 6,
            "Ch. 7 - Return": 7,
            "Episode 8 - Return": 8,
            "第9章 帰還": 9,
            "第10話 帰還": 10,
        }

        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(detect_chapter_number(title), expected)

    def test_ignores_unmarked_numbers(self) -> None:
        cases = [
            "notice 65",
            "일러스트 모음 65 추가",
            "2024 special notice",
            "cover",
        ]

        for title in cases:
            with self.subTest(title=title):
                self.assertIsNone(detect_chapter_number(title))

    def test_falls_back_to_reading_order_for_unnumbered_titles(self) -> None:
        sections = [
            section(1, "해방노예인데 주인이 집착한다", text="Author: a\nTags: b\nSynopsis\n..."),
            section(2, "Notice: 르노아 일러스트 모음"),
            section(3, "해방된 노예가 집착할 리 없잖아"),
            section(4, "주인은 노예 해방에 목숨 거는 거 아니었냐고"),
        ]

        chapters = select_processed_chapters(sections)

        self.assertEqual([chapter.number for chapter in chapters], [1, 2])
        self.assertEqual(
            [chapter.section.title for chapter in chapters],
            [
                "해방된 노예가 집착할 리 없잖아",
                "주인은 노예 해방에 목숨 거는 거 아니었냐고",
            ],
        )

    def test_notice_chapter_markers_do_not_disable_fallback(self) -> None:
        sections = [
            section(1, "cover"),
            section(2, "Demo Book", text="Author: a\nTags: b\nSynopsis\n..."),
            section(3, "Notice: 550화까지 왔습니다!!"),
            section(4, "Notice: 259화 삽화 추가되었습니다!!!"),
            section(5, "1.[첫 번째 이야기]"),
            section(6, "2.[두 번째 이야기]"),
        ]

        chapters = select_processed_chapters(sections)

        self.assertEqual([chapter.number for chapter in chapters], [1, 2])
        self.assertEqual(
            [chapter.section.title for chapter in chapters],
            ["1.[첫 번째 이야기]", "2.[두 번째 이야기]"],
        )

    def test_import_writes_shared_input_and_metadata_with_name_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            epub_path = root / "demo.epub"
            write_epub(
                epub_path,
                title="Demo EPUB Title",
                author="Demo Author",
                sections=[
                    ("chapter-1.xhtml", "Chapter 1: Start", "Hello world."),
                    ("chapter-2.xhtml", "Chapter 2: Next", "Second chapter."),
                ],
            )

            result = import_epub(epub_path, root / "share", name="Military Training")
            novel_dir = root / "share" / "military-training"
            chapter_dir = novel_dir / "input"
            metadata = json.loads((novel_dir / "metadata.json").read_text(encoding="utf-8"))
            chapter_one = (chapter_dir / "chapter_1.txt").read_text(encoding="utf-8")

        self.assertEqual(result.output_dir, str(novel_dir))
        self.assertEqual(result.chapter_output_dir, str(chapter_dir))
        self.assertEqual(len(result.chapters), 2)
        self.assertEqual(
            metadata,
            {
                "title": "Demo EPUB Title",
                "translated": {"en": None, "vi": None},
                "author": "Demo Author",
                "source_url": epub_path.resolve().as_uri(),
                "illustration_url": None,
                "site_name": "military-training",
            },
        )
        self.assertIn("Chapter 1: Start", chapter_one)
        self.assertIn("Hello world.", chapter_one)

    def test_import_defaults_output_slug_to_filename_not_epub_title(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            epub_path = root / "downloaded-book.epub"
            write_epub(
                epub_path,
                title="Completely Different EPUB Title",
                author=None,
                sections=[("chapter-1.xhtml", "Chapter 1: Start", "Hello world.")],
            )

            result = import_epub(epub_path, root / "share")

        self.assertEqual(result.output_dir, str(root / "share" / "downloaded-book"))
        self.assertEqual(result.metadata.title, "Completely Different EPUB Title")

    def test_import_writes_illustrations_with_order_and_chapter_number(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            epub_path = root / "illustrated.epub"
            write_epub(
                epub_path,
                title="Illustrated",
                author=None,
                sections=[
                    ("Text/chapter-1.xhtml", "Chapter 1: Start", "Hello world."),
                    ("Text/chapter-2.xhtml", "Chapter 2: Next", "Second chapter."),
                ],
                section_images={
                    "Text/chapter-1.xhtml": ["../Images/first.jpg"],
                    "Text/chapter-2.xhtml": ["../Images/second.png", "../Images/third.webp"],
                },
                image_files={
                    "OPS/Images/first.jpg": b"first-image",
                    "OPS/Images/second.png": b"second-image",
                    "OPS/Images/third.webp": b"third-image",
                },
            )

            result = import_epub(epub_path, root / "share", name="Illustrated")
            illustrations_dir = root / "share" / "illustrated" / "illustrations"
            first_image = (illustrations_dir / "001-001.jpg").read_bytes()
            second_image = (illustrations_dir / "002-001.png").read_bytes()
            chapter_one = (
                root / "share" / "illustrated" / "input" / "chapter_1.txt"
            ).read_text(encoding="utf-8")
            chapter_two = (
                root / "share" / "illustrated" / "input" / "chapter_2.txt"
            ).read_text(encoding="utf-8")

        self.assertEqual(
            [Path(illustration.path).name for illustration in result.illustrations],
            [
                "001-001.jpg",
                "002-001.png",
                "002-002.webp",
            ],
        )
        self.assertEqual(
            [illustration.chapter_number for illustration in result.illustrations],
            [1, 2, 2],
        )
        self.assertEqual(first_image, b"first-image")
        self.assertEqual(second_image, b"second-image")
        self.assertIn("Hello world.\n\n[[ILLUSTRATION:001-001.jpg]]", chapter_one)
        self.assertIn(
            "Second chapter.\n\n[[ILLUSTRATION:002-001.png]]\n\n"
            "[[ILLUSTRATION:002-002.webp]]",
            chapter_two,
        )

    def test_import_falls_back_to_name_and_cleans_existing_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            epub_path = root / "untitled.epub"
            write_epub(
                epub_path,
                title=None,
                author=None,
                sections=[
                    ("intro.xhtml", "Unnumbered opening", "Story text."),
                ],
            )
            stale_path = root / "share" / "manual-name" / "input" / "chapter_99.txt"
            stale_path.parent.mkdir(parents=True)
            stale_path.write_text("stale", encoding="utf-8")

            result = import_epub(epub_path, root / "share", name="Manual Name")
            metadata = json.loads(
                (root / "share" / "manual-name" / "metadata.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result.metadata.title, "Manual Name")
            self.assertEqual(metadata["title"], "Manual Name")
            self.assertFalse(stale_path.exists())
            self.assertTrue((stale_path.parent / "chapter_1.txt").is_file())


def section(index: int, title: str, text: str = "body") -> EpubSection:
    return EpubSection(
        index=index,
        source_path=f"section-{index}.xhtml",
        title=title,
        text=text,
    )


def write_epub(
    path: Path,
    *,
    title: str | None,
    author: str | None,
    sections: list[tuple[str, str, str]],
    section_images: dict[str, list[str]] | None = None,
    image_files: dict[str, bytes] | None = None,
) -> None:
    section_images = section_images or {}
    image_files = image_files or {}
    metadata = []
    if title is not None:
        metadata.append(f"<dc:title>{escape(title)}</dc:title>")
    if author is not None:
        metadata.append(f"<dc:creator>{escape(author)}</dc:creator>")

    manifest = []
    spine = []
    for index, (href, _section_title, _text) in enumerate(sections, start=1):
        item_id = f"section-{index}"
        manifest.append(
            f'<item id="{item_id}" href="{escape(href)}" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="{item_id}"/>')

    content_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    {''.join(metadata)}
  </metadata>
  <manifest>
    {''.join(manifest)}
  </manifest>
  <spine>
    {''.join(spine)}
  </spine>
</package>
"""
    container_xml = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    with zipfile.ZipFile(path, "w") as epub:
        epub.writestr("META-INF/container.xml", container_xml)
        epub.writestr("OPS/content.opf", content_opf)
        for href, section_title, text in sections:
            image_markup = "".join(
                f'<img src="{escape(image_ref)}" alt=""/>'
                for image_ref in section_images.get(href, [])
            )
            epub.writestr(
                f"OPS/{href}",
                (
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<html xmlns="http://www.w3.org/1999/xhtml">'
                    f"<body><h1>{escape(section_title)}</h1><p>{escape(text)}</p>"
                    f"{image_markup}</body>"
                    "</html>"
                ),
            )
        for image_path, image_data in image_files.items():
            epub.writestr(image_path, image_data)


if __name__ == "__main__":
    unittest.main()
