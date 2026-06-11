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


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


@dataclass
class Config:
    """Application-level configuration from environment."""

    share_dir: str | None = None
    max_chapters: int = 0  # 0 = no limit
    use_browser: bool = False
    llm_provider: str = "ollama"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    @property
    def share_path(self) -> Path | None:
        return Path(self.share_dir).expanduser() if self.share_dir else None

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            share_dir=os.getenv("NOVEL_SHARE_DIR") or None,
            max_chapters=int(os.getenv("MAX_CHAPTERS") or "0"),
            use_browser=(os.getenv("USE_BROWSER") or "false").lower() in ("true", "1", "yes"),
            llm_provider=os.getenv("LLM_PROVIDER") or "ollama",
            llm_temperature=float(os.getenv("LLM_TEMPERATURE") or "0.0"),
            llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS") or "4096"),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434",
            ollama_model=os.getenv("OLLAMA_MODEL") or "llama3",
            gemini_api_key=os.getenv("GEMINI_API_KEY") or "",
            gemini_model=os.getenv("GEMINI_MODEL") or "gemini-2.5-flash",
        )


config = Config.from_env()


@dataclass(frozen=True)
class SiteConfig:
    """Per-site configuration from JSON file."""

    name: str
    start_url: str
    chapter_link_selector: str
    chapter_content_selector: str
    version: int = 1
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
        data = cls._migrate(data)

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
            version=int(data.get("version", 1)),
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

    @staticmethod
    def _migrate(data: dict[str, Any]) -> dict[str, Any]:
        """Migrate older config schemas to the current version."""
        version_val = data.get("version", 1)
        try:
            version = int(version_val)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid config version: {version_val}") from e

        if version < 1:
            data["version"] = 1
        elif version > 1:
            raise ValueError(
                f"Unsupported future config version: {version}. "
                f"Current schema version is 1."
            )
        # Future migrations go here:
        # if version < 2:
        #     data.setdefault("new_field", "default")
        #     data["version"] = 2
        return data


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
