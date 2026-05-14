"""
Configuration for Novel Crawler.
Loads settings from .env file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(interpolate=True)


DEFAULT_USER_AGENT = "novel-crawler/0.1 (+https://example.local)"


@dataclass
class Config:
    """Application-level configuration from environment."""

    share_dir: str | None = None
    max_chapters: int = 0  # 0 = no limit
    use_browser: bool = False

    @property
    def share_path(self) -> Path | None:
        return Path(self.share_dir).expanduser() if self.share_dir else None

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            share_dir=os.getenv("NOVEL_SHARE_DIR") or None,
            max_chapters=int(os.getenv("MAX_CHAPTERS") or "0"),
            use_browser=(os.getenv("USE_BROWSER") or "false").lower() in ("true", "1", "yes"),
        )


config = Config.from_env()


@dataclass(frozen=True)
class SiteConfig:
    """Per-site configuration from JSON file."""

    name: str
    start_url: str
    chapter_link_selector: str
    chapter_content_selector: str
    novel_title_selector: str | None = None
    author_selector: str | None = None
    toc_next_selector: str | None = None
    chapter_title_selector: str | None = None
    remove_selectors: tuple[str, ...] = ()
    same_domain: bool = True
    reverse_chapter_order: bool = False
    request_delay_seconds: float = 1.0
    timeout_seconds: float = 30.0
    retry_attempts: int = 3
    retry_backoff_seconds: float = 2.0
    max_toc_pages: int = 50
    user_agent: str = DEFAULT_USER_AGENT

    @classmethod
    def from_file(cls, path: Any) -> SiteConfig:
        with Path(path).open("r", encoding="utf-8") as config_file:
            data = json.load(config_file)
        if not isinstance(data, dict):
            raise ValueError("Config file must contain a JSON object.")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiteConfig:
        required = [
            "name",
            "start_url",
            "chapter_link_selector",
            "chapter_content_selector",
        ]
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise ValueError(f"Missing required config fields: {', '.join(missing)}")

        remove_selectors = data.get("remove_selectors", ())
        if isinstance(remove_selectors, str):
            remove_selectors = [remove_selectors]
        if not isinstance(remove_selectors, (list, tuple)):
            raise ValueError("remove_selectors must be a list of CSS selectors.")

        return cls(
            name=str(data["name"]),
            start_url=str(data["start_url"]),
            chapter_link_selector=str(data["chapter_link_selector"]),
            chapter_content_selector=str(data["chapter_content_selector"]),
            novel_title_selector=_optional_str(data.get("novel_title_selector")),
            author_selector=_optional_str(data.get("author_selector")),
            toc_next_selector=_optional_str(data.get("toc_next_selector")),
            chapter_title_selector=_optional_str(data.get("chapter_title_selector")),
            remove_selectors=tuple(str(selector) for selector in remove_selectors),
            same_domain=bool(data.get("same_domain", True)),
            reverse_chapter_order=bool(data.get("reverse_chapter_order", False)),
            request_delay_seconds=float(data.get("request_delay_seconds", 1.0)),
            timeout_seconds=float(data.get("timeout_seconds", 30.0)),
            retry_attempts=int(data.get("retry_attempts", 3)),
            retry_backoff_seconds=float(data.get("retry_backoff_seconds", 2.0)),
            max_toc_pages=int(data.get("max_toc_pages", 50)),
            user_agent=str(data.get("user_agent", DEFAULT_USER_AGENT)),
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
