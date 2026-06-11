"""HTML cleaning utilities for LLM analysis.

Strips noise (scripts, styles, inline attributes) while preserving
structural information (tags, ids, classes, hrefs) so the LLM can
identify the correct CSS selectors.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, Tag
from bs4.element import NavigableString

# Attributes worth keeping for selector analysis.
_KEEP_ATTRS = {"id", "class", "href", "role", "type", "name"}

# Tags that are pure noise for structural analysis.
_REMOVE_TAGS = {"script", "style", "noscript", "svg", "iframe", "link", "meta"}


def clean_html_for_analysis(html: str, *, max_length: int = 30_000) -> str:
    """Clean HTML for LLM analysis: keep structure, remove noise.

    Steps:
        1. Remove <script>, <style>, comments, etc.
        2. Strip non-structural attributes (inline styles, data-*, event handlers)
        3. Truncate long text nodes (>80 chars → first 40 + "…")
        4. Collapse excessive whitespace
        5. Limit total output to ``max_length`` characters
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1. Remove noise tags.
    for tag in soup.find_all(_REMOVE_TAGS):
        tag.decompose()

    # 2. Remove HTML comments.
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    # 3. Strip non-structural attributes.
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        attrs_to_remove = [
            attr for attr in tag.attrs if attr not in _KEEP_ATTRS
        ]
        for attr in attrs_to_remove:
            del tag[attr]

    # 4. Truncate long text nodes.
    for text_node in soup.find_all(string=True):
        if not isinstance(text_node, NavigableString):
            continue
        original = str(text_node).strip()
        if len(original) > 80:
            truncated = original[:40] + "…"
            text_node.replace_with(truncated)

    # 5. Collapse whitespace and limit length.
    result = soup.prettify()
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = re.sub(r"[ \t]+\n", "\n", result)

    if len(result) > max_length:
        result = result[:max_length] + "\n<!-- truncated -->"

    return result
