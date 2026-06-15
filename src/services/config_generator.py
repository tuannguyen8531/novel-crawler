"""AI-assisted site config generator.

Given a TOC URL, fetches the page HTML, sends it to an LLM for structural
analysis, then repeats with a sample chapter page.  The result is a
validated ``SiteConfig`` ready to write to disk.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.config import DEFAULT_USER_AGENT, SiteConfig
from src.services.http import FetchResponse, HttpClient
from src.services.llm.base import BaseProvider
from src.utils.html import clean_html_for_analysis
from src.utils.logging import get_logger


class Fetcher(Protocol):
    """Minimal interface shared by HttpClient and BrowserFetcher."""
    def fetch(self, url: str) -> FetchResponse: ...


class _HtmlCache:
    """Simple file-based cache for raw HTML responses.

    Automatically invalidates entries that look like error or challenge pages.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:16] + ".html"

    def get(self, url: str) -> str | None:
        path = self._dir / self._key(url)
        if not path.is_file():
            return None
        html = path.read_text(encoding="utf-8")
        if self._is_bad(html):
            print(f"   ⚠  Cached HTML looks bad — invalidating cache for {url}")
            self.invalidate(url)
            return None
        return html

    def set(self, url: str, html: str) -> None:
        path = self._dir / self._key(url)
        path.write_text(html, encoding="utf-8")

    def invalidate(self, url: str) -> None:
        path = self._dir / self._key(url)
        if path.exists():
            path.unlink()

    @staticmethod
    def _is_bad(html: str) -> bool:
        """Detect if cached HTML is useless (error, challenge, or empty)."""
        if not html or len(html.strip()) < 200:
            return True
        soup = BeautifulSoup(html, "html.parser")
        title = (soup.title.string or "").lower() if soup.title else ""
        body_text = soup.get_text(" ", strip=True)[:300].lower()

        bad_signals = (
            "just a moment",
            "checking your browser",
            "ddos protection",
            "attention required",
            "cloudflare",
            "404",
            "not found",
            "error",
            "access denied",
            "forbidden",
        )
        return any(sig in title for sig in bad_signals) or any(
            sig in body_text for sig in bad_signals
        )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_TOC = """\
You are an expert web scraper assistant.  Given the **cleaned HTML** of a \
novel's Table-of-Contents page and its URL, identify the correct CSS selectors.

Return **only** a JSON object (no markdown fences) with these keys:

{
  "novel_title_selector": "<CSS selector for the novel title, or null>",
  "author_selector": "<CSS selector for the author name, or null>",
  "chapter_link_selector": "<CSS selector that matches ALL chapter <a> links>",
  "toc_next_selector": "<CSS selector for the 'next page' link if TOC is paginated, or null>"
}

Rules:
- Prefer **id** selectors (e.g. ``#catalog``) or **specific class** chains (e.g. ``#catalog ul li a``) over bare tag names or generic classes like ``.main-content``.
- ``chapter_link_selector`` must match <a> elements whose ``href`` points to individual chapter pages. It should NOT match unrelated links (home, profile, ads).
- ``novel_title_selector`` should target the actual novel name, usually inside an ``<h1>`` or a breadcrumb. Example: ``#catalog h1 a``.
- If you cannot determine a selector, set its value to ``null``.
- Output **pure JSON only** — no commentary, no markdown.

Example for a typical Chinese novel site:
{
  "novel_title_selector": "#catalog h1 a",
  "author_selector": null,
  "chapter_link_selector": "#catalog ul li a",
  "toc_next_selector": null
}\
"""

_SYSTEM_CHAPTER = """\
You are an expert web scraper assistant.  Given the **cleaned HTML** of a \
single chapter page and its URL, identify CSS selectors for extracting the \
chapter content.

Return **only** a JSON object (no markdown fences) with these keys:

{
  "chapter_title_selector": "<CSS selector for the chapter title, or null>",
  "chapter_content_selector": "<CSS selector for the main reading content>",
  "remove_selectors": ["<list of CSS selectors for elements to remove>"]
}

Rules:
- ``chapter_content_selector`` is the **single smallest container** holding the story text. Avoid ``body`` or ``.main-content`` if a more specific inner container exists (e.g. ``.txtnav`` or ``#ChapterBody``).
- ``chapter_title_selector`` targets the chapter heading (often ``<h1>``). If that heading sits **inside** the content container, you MUST also include the title selector in ``remove_selectors`` so it does not appear twice in the extracted text.
- ``remove_selectors`` must always include ``"script"`` and ``"style"``. Also add: ads (``.ad``, ``.ads``, ``.contentadv``), navigation links (``.page1``, ``.next-chapter``, ``#txtright``), share buttons, author/info blocks (``.txtinfo``, ``.readinline``), and any other non-story elements inside the content container.
- Prefer selectors using **id** or **class**.
- Output **pure JSON only** — no commentary, no markdown.

Example for a typical Chinese novel site:
{
  "chapter_title_selector": ".txtnav h1",
  "chapter_content_selector": ".txtnav",
  "remove_selectors": [
    "script",
    "style",
    ".txtnav h1",
    ".txtinfo",
    "#txtright",
    ".contentadv",
    ".bottom-ad",
    ".page1",
    ".readinline"
  ]
}\
"""

_RETRY_TOC = """\
Your previous selectors did not match any elements in the provided HTML.

Please look again at the cleaned HTML and return corrected selectors.
Pay special attention to:
- The list of chapter links — what ``id`` or ``class`` wraps the <ul> or <ol> of links?
- The novel title — is it inside ``<h1>`` or a breadcrumb?

Return **only** the JSON object, no markdown.\
"""

_RETRY_CHAPTER = """\
Your previous selectors did not match any elements in the provided HTML.

Please look again at the cleaned HTML and return corrected selectors.
Pay special attention to:
- The smallest container that holds **only** the story text.
- If the chapter title is inside that container, include its selector in ``remove_selectors``.
- Remove ads, navigation, share buttons, and any non-story markup.

Return **only** the JSON object, no markdown.\
"""


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class ConfigGenerator:
    """Two-phase AI config generator with validation and retry."""

    def __init__(
        self,
        llm: BaseProvider,
        *,
        use_browser: bool = False,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._llm = llm
        self._use_browser = use_browser
        self._user_agent = user_agent

    # -- public API ---------------------------------------------------------

    def generate(
        self,
        toc_url: str,
        *,
        name: str | None = None,
        configs_dir: Path | None = None,
        cache_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Run both phases and return a complete config dict.

        Returns the raw dict (not yet a SiteConfig) so the caller can
        review / edit before persisting.
        """
        configs_dir = configs_dir or Path("configs")
        cache = _HtmlCache(cache_dir or Path("data") / ".gen-cache")
        domain = urlparse(toc_url).netloc
        known = self._load_known_domain_config(domain, configs_dir)

        with self._open_fetcher() as fetcher:
            # Phase 1: TOC analysis
            toc_html = self._fetch_or_cache(fetcher, cache, toc_url, "TOC")
            if toc_html is None:
                raise RuntimeError(f"Failed to fetch TOC page: {toc_url}")

            # Detect 404 / error pages and retry with trailing slash.
            if self._is_error_page(toc_html) and not toc_url.endswith("/"):
                alt_url = toc_url.rstrip("/") + "/"
                print(f"⚠  Page looks like a 404 — retrying with: {alt_url}")
                alt_html = self._fetch_or_cache(fetcher, cache, alt_url, "TOC")
                if alt_html is not None and not self._is_error_page(alt_html):
                    toc_html = alt_html
                    toc_url = alt_url
                else:
                    get_logger().warning("Still a 404 — proceeding with original page.")

            toc_soup = BeautifulSoup(toc_html, "html.parser")
            toc_clean = clean_html_for_analysis(toc_html)

            # Try known-domain selectors first; fall back to LLM.
            toc_result = self._try_known_selectors(
                known, toc_soup, "toc", toc_clean, _SYSTEM_TOC, _RETRY_TOC
            )

            # Discover first chapter URL for Phase 2
            chapter_url = self._find_first_chapter(
                toc_soup, toc_url, toc_result.get("chapter_link_selector", "")
            )

            chapter_result: dict[str, Any] = {}
            if chapter_url:
                # Phase 2: Chapter analysis with automatic browser fallback
                ch_html, ch_soup = self._fetch_chapter_with_fallback(
                    fetcher, chapter_url, cache
                )
                if ch_soup is not None:
                    ch_clean = clean_html_for_analysis(ch_html)
                    chapter_result = self._try_known_selectors(
                        known, ch_soup, "chapter", ch_clean, _SYSTEM_CHAPTER, _RETRY_CHAPTER
                    )
                else:
                    get_logger().warning(
                        "Could not fetch chapter content even with browser — skipping Phase 2."
                    )
            else:
                get_logger().warning("Could not find a chapter link — skipping Phase 2.")

        # Merge results
        site_name = name or self._derive_name(toc_url)
        config_dict = self._build_config(toc_url, site_name, toc_result, chapter_result)
        return config_dict

    def _try_known_selectors(
        self,
        known: dict[str, Any] | None,
        soup: BeautifulSoup,
        phase: str,
        clean_html: str,
        system_prompt: str,
        retry_prompt: str,
    ) -> dict[str, Any]:
        """Use known-domain selectors if they validate, else ask the LLM."""
        if known:
            result = (
                {
                    "novel_title_selector": known.get("novel_title_selector"),
                    "author_selector": known.get("author_selector"),
                    "chapter_link_selector": known.get("chapter_link_selector"),
                    "toc_next_selector": known.get("toc_next_selector"),
                }
                if phase == "toc"
                else {
                    "chapter_title_selector": known.get("chapter_title_selector"),
                    "chapter_content_selector": known.get("chapter_content_selector"),
                    "remove_selectors": list(known.get("remove_selectors", [])),
                }
            )
            issues = self._validate_selectors(result, soup, f"gen-config-{phase}")
            if not issues:
                print(f"✅ Reusing known {phase} selectors for this domain.")
                return result
            print(
                f"⚠  Known {phase} selectors stale ({', '.join(issues)}) — falling back to LLM."
            )

        return self._ask_llm_with_retry(
            system=system_prompt,
            user=f"HTML:\n{clean_html}",
            call_type=f"gen-config-{phase}",
            soup=soup,
            retry_system=retry_prompt,
        )

    @staticmethod
    def validate(config_dict: dict[str, Any]) -> SiteConfig:
        """Validate a config dict by constructing a SiteConfig."""
        return SiteConfig.from_dict(config_dict)

    @staticmethod
    def save(config_dict: dict[str, Any], output_dir: Path) -> Path:
        """Write config JSON to disk and return the path."""
        name = config_dict.get("name", "generated")
        filename = f"{name}.json"
        path = output_dir / filename
        output_dir.mkdir(parents=True, exist_ok=True)
        content = json.dumps(config_dict, ensure_ascii=False, indent=2) + "\n"
        path.write_text(content, encoding="utf-8")
        return path

    # -- private helpers ----------------------------------------------------

    @contextmanager
    def _open_fetcher(self) -> Generator[Fetcher]:
        """Yield a fetcher, using context manager for BrowserFetcher."""
        if self._use_browser:
            from src.services.browser import BrowserFetcher

            with BrowserFetcher(
                user_agent=self._user_agent,
                timeout_seconds=30,
                delay_seconds=1.0,
            ) as fetcher:
                yield fetcher
        else:
            yield HttpClient(
                user_agent=self._user_agent,
                timeout_seconds=30,
                delay_seconds=1.5,
                respect_robots=False,
            )

    def _fetch_or_cache(
        self,
        fetcher: Fetcher,
        cache: _HtmlCache,
        url: str,
        label: str,
    ) -> str | None:
        """Return cached HTML if present, else fetch and cache."""
        cached = cache.get(url)
        if cached is not None:
            print(f"📦 {label} cache hit: {url}")
            return cached

        print(f"🌐 {label} cache miss — fetching: {url}")
        try:
            response = fetcher.fetch(url)
            cache.set(url, response.body)
            return response.body
        except Exception as e:
            get_logger().warning("Failed to fetch %s: %s", url, e)
            return None

    def _fetch_chapter_with_fallback(
        self,
        fetcher: Fetcher,
        chapter_url: str,
        cache: _HtmlCache,
    ) -> tuple[str, BeautifulSoup | None]:
        """Fetch a chapter page; fallback to browser if blocked by anti-bot."""
        ch_html = self._fetch_or_cache(fetcher, cache, chapter_url, "Chapter")
        if ch_html is None:
            return "", None
        ch_soup = BeautifulSoup(ch_html, "html.parser")

        if not self._is_challenge_page(ch_html):
            return ch_html, ch_soup

        get_logger().warning(
            "Chapter page looks like an anti-bot challenge — trying browser fallback..."
        )
        if self._use_browser:
            get_logger().warning(
                "Already using browser, but still got a challenge page. Proceeding anyway."
            )
            return ch_html, ch_soup

        from src.services.browser import BrowserFetcher

        with BrowserFetcher(
            user_agent=self._user_agent,
            timeout_seconds=30,
            delay_seconds=1.0,
        ) as browser_fetcher:
            ch_html = browser_fetcher.fetch(chapter_url).body
            cache.set(chapter_url, ch_html)
            ch_soup = BeautifulSoup(ch_html, "html.parser")
            if self._is_challenge_page(ch_html):
                get_logger().warning(
                    "Browser also hit a challenge page. Site may require advanced bypass."
                )
                return ch_html, None
            get_logger().info("Browser fetch succeeded.")
            return ch_html, ch_soup

    @staticmethod
    def _is_error_page(html: str) -> bool:
        """Detect if the fetched page is a 404 or error page."""
        soup = BeautifulSoup(html, "html.parser")
        title = (soup.title.string or "") if soup.title else ""
        title_lower = title.lower()
        # Common 404 indicators in page title.
        if any(sig in title_lower for sig in ("404", "not found", "错误", "不存在")):
            return True
        # Check body text for error messages.
        body_text = soup.get_text(" ", strip=True)[:500].lower()
        if any(sig in body_text for sig in ("页面不存在", "页面已删除", "page not found")):
            return True
        return False

    @staticmethod
    def _load_known_domain_config(
        domain: str, configs_dir: Path
    ) -> dict[str, Any] | None:
        """Scan configs_dir for a config whose start_url netloc matches domain."""
        if not configs_dir.is_dir():
            return None
        for path in sorted(configs_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                start_url = data.get("start_url", "")
                if urlparse(start_url).netloc == domain:
                    return data
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def _is_challenge_page(html: str) -> bool:
        """Detect Cloudflare or anti-bot challenge pages."""
        soup = BeautifulSoup(html, "html.parser")
        title = (soup.title.string or "") if soup.title else ""
        title_lower = title.lower()

        challenge_titles = (
            "just a moment",
            "checking your browser",
            "ddos protection",
            "attention required",
            "cloudflare",
            "wait",
        )
        if any(sig in title_lower for sig in challenge_titles):
            return True

        body_text = soup.get_text(" ", strip=True)[:500].lower()
        challenge_texts = (
            "just a moment",
            "checking your browser",
            "ddos protection",
            "cloudflare",
            "please enable javascript",
            "please wait",
            "redirecting",
        )
        if any(sig in body_text for sig in challenge_texts):
            return True

        # Very small body with known challenge wrapper.
        if len(html) < 1500 and soup.select_one(".main-wrapper, #cf-wrapper, #challenge-form"):
            return True

        return False

    def _ask_llm_with_retry(
        self,
        *,
        system: str,
        user: str,
        call_type: str,
        soup: BeautifulSoup,
        retry_system: str,
        max_retries: int = 1,
    ) -> dict[str, Any]:
        """Send prompt to LLM, validate selectors, and retry once if they fail."""
        result = self._ask_llm(system=system, user=user, call_type=call_type)
        issues = self._validate_selectors(result, soup, call_type)

        if issues and max_retries > 0:
            print(f"⚠  Selector issues detected — retrying ({', '.join(issues)})")
            retry_user = (
                f"Previous issues: {', '.join(issues)}\n\n"
                f"{user}"
            )
            result = self._ask_llm(
                system=retry_system, user=retry_user, call_type=f"{call_type}-retry"
            )
            issues = self._validate_selectors(result, soup, call_type)
            if issues:
                print(f"⚠  Still has issues after retry: {', '.join(issues)}")

        return result

    def _ask_llm(self, *, system: str, user: str, call_type: str) -> dict[str, Any]:
        """Send prompt to LLM and parse JSON response."""
        raw = self._llm.generate(system, user, call_type)
        return self._parse_json(raw)

    @staticmethod
    def _validate_selectors(
        result: dict[str, Any], soup: BeautifulSoup, call_type: str
    ) -> list[str]:
        """Check that returned selectors actually match elements in the HTML."""
        issues: list[str] = []

        if call_type.startswith("gen-config-toc"):
            for key in ("chapter_link_selector", "novel_title_selector"):
                selector = result.get(key)
                if selector and not soup.select(selector):
                    issues.append(f"{key} matches 0 elements")
        elif call_type.startswith("gen-config-chapter"):
            content_sel = result.get("chapter_content_selector")
            if content_sel and not soup.select(content_sel):
                issues.append("chapter_content_selector matches 0 elements")
            for key in ("chapter_title_selector",):
                selector = result.get(key)
                if selector and not soup.select(selector):
                    issues.append(f"{key} matches 0 elements")

        return issues

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """Extract the first JSON object from LLM output."""
        # Try direct parse first.
        text = text.strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass

        # Strip markdown code fences if present.
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Last resort: find first { … } block.
        brace_match = re.search(r"\{.*}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"LLM output is not valid JSON:\n{text[:500]}")

    @staticmethod
    def _find_first_chapter(
        soup: BeautifulSoup, base_url: str, selector: str
    ) -> str | None:
        """Use the LLM-suggested selector to find the first chapter link."""
        if not selector:
            return None
        anchors = soup.select(selector)
        base_netloc = urlparse(base_url).netloc
        for anchor in anchors:
            href = anchor.get("href")
            if not isinstance(href, str) or not href:
                continue
            url = urljoin(base_url, href)
            # basic sanity: same domain
            if urlparse(url).netloc == base_netloc:
                return url
        return None

    @staticmethod
    def _derive_name(url: str) -> str:
        """Derive a short config name from the URL."""
        parsed = urlparse(url)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if parts:
            return parts[-1].rstrip("/")
        return parsed.netloc.replace(".", "-")

    @staticmethod
    def _build_config(
        toc_url: str,
        name: str,
        toc: dict[str, Any],
        chapter: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge Phase 1 + Phase 2 results into a full config dict."""
        remove = list(chapter.get("remove_selectors") or ["script", "style"])
        if "script" not in remove:
            remove.insert(0, "script")
        if "style" not in remove:
            remove.insert(1, "style")

        # Deduplicate while preserving order.
        seen: set[str] = set()
        deduped = [s for s in remove if not (s in seen or seen.add(s))]

        # Smart default for max_toc_pages: 1 when no pagination, else 50.
        toc_next = toc.get("toc_next_selector")
        max_toc_pages = 1 if toc_next is None else 50

        # Handle null values from LLM — .get(key, default) returns None when
        # the key exists with a null value, so we must normalise explicitly.
        def _or(val: Any, default: Any) -> Any:
            return val if val is not None else default

        return {
            "name": name,
            "start_url": toc_url,
            "version": 1,
            "novel_title_selector": _or(toc.get("novel_title_selector"), None),
            "author_selector": _or(toc.get("author_selector"), None),
            "chapter_link_selector": _or(toc.get("chapter_link_selector"), "a"),
            "toc_next_selector": _or(toc_next, None),
            "chapter_title_selector": _or(chapter.get("chapter_title_selector"), None),
            "chapter_content_selector": _or(chapter.get("chapter_content_selector"), "body"),
            "remove_selectors": deduped,
            "same_domain": True,
            "reverse_chapter_order": False,
            "filter_non_chapter_links": True,
            "request_delay_seconds": 2.0,
            "timeout_seconds": 30,
            "max_toc_pages": max_toc_pages,
            "user_agent": DEFAULT_USER_AGENT,
        }
