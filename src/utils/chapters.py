"""Shared chapter-title classification for crawl and EPUB import workflows."""

from __future__ import annotations

import re
from collections.abc import Callable

CHAPTER_PATTERNS = (
    re.compile(r"(?<!\d)(?:제\s*)?(\d+)\s*(?:화|장)(?!\d)", re.IGNORECASE),
    re.compile(r"第\s*(\d+)\s*[章节話话回]", re.IGNORECASE),
    re.compile(r"\b(?:chương|chuong)\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\b(?:chapter|chap\.?|ch\.?)\s*#?\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\b(?:episode|ep\.?)\s*#?\s*(\d+)\b", re.IGNORECASE),
)

NOTICE_MARKERS = (
    "notice",
    "announcement",
    "공지",
    "公告",
    "通知",
)

def detect_chapter_number(title: str) -> int | None:
    """Return an explicit chapter number from a supported title format."""
    normalized_title = " ".join(title.split())
    for pattern in CHAPTER_PATTERNS:
        match = pattern.search(normalized_title)
        if match:
            return int(match.group(1))
    return None


def is_obvious_non_chapter_title(title: str) -> bool:
    """Return whether a title is clearly an announcement rather than story content."""
    normalized_title = " ".join(title.split()).casefold()
    return any(marker in normalized_title for marker in NOTICE_MARKERS)


def select_likely_chapters[T](
    items: list[T],
    *,
    title_getter: Callable[[T], str],
) -> list[T]:
    """Filter notices and prefer explicit chapter markers when the list has them."""
    candidates = [
        item
        for item in items
        if not is_obvious_non_chapter_title(title_getter(item))
    ]
    explicit_chapters = [
        item
        for item in candidates
        if detect_chapter_number(title_getter(item)) is not None
    ]
    return explicit_chapters or candidates
