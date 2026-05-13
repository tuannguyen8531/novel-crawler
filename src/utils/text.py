from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup, Tag

_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTIPLE_BLANK_LINES = re.compile(r"\n{3,}")
_SPACE_AROUND_NEWLINES = re.compile(r"[ \t]*\n[ \t]*")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")


def slugify(value: str, fallback: str = "novel") -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = _BAD_FILENAME_CHARS.sub("-", ascii_text.lower())
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    cleaned = cleaned.strip(".-_")
    return cleaned or fallback


def normalize_text(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = _WHITESPACE.sub(" ", value)
    return value.strip()


def html_to_plain_text(container: Tag) -> str:
    soup = BeautifulSoup(str(container), "html.parser")
    for element in soup.select("script, style, noscript"):
        element.decompose()
    for br in soup.select("br"):
        br.replace_with("\n")

    text = soup.get_text("\n")
    text = _SPACE_AROUND_NEWLINES.sub("\n", text)
    lines = [normalize_text(line) for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return _MULTIPLE_BLANK_LINES.sub("\n\n", text).strip()
