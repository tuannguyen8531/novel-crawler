from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChapterLink:
    title: str
    url: str


@dataclass(frozen=True)
class NovelMetadata:
    title: str
    author: str | None
    source_url: str
    site_name: str
    translated: dict[str, str | None] = field(
        default_factory=lambda: {"en": None, "vi": None}
    )
    illustration_url: str | None = None


@dataclass(frozen=True)
class ChapterResult:
    index: int
    title: str
    source_url: str
    path: str
    skipped: bool = False


@dataclass(frozen=True)
class CrawlResult:
    metadata: NovelMetadata
    chapters: list[ChapterResult]
    output_dir: str
    chapter_output_dir: str


@dataclass(frozen=True)
class CrawlProgress:
    current: int
    total: int
    status: str
    title: str
    source_url: str
    path: str | None = None
    error: str | None = None
