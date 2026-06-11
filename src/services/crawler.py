from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.config import SiteConfig
from src.models import (
    ChapterLink,
    ChapterResult,
    CrawlProgress,
    CrawlResult,
    NovelMetadata,
)
from src.services.http import FetchError, FetchResponse, HttpClient
from src.utils.text import html_to_plain_text, normalize_text, slugify

ProgressCallback = Callable[[CrawlProgress], None]


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchResponse: ...


class CrawlError(TypedDict):
    index: int
    url: str
    error: str


class NovelCrawler:
    def __init__(
        self,
        config: SiteConfig,
        *,
        respect_robots: bool = True,
        fetcher: Fetcher | None = None,
    ) -> None:
        self.config = config
        self.client: Fetcher = fetcher or HttpClient(
            user_agent=config.user_agent,
            timeout_seconds=config.timeout_seconds,
            delay_seconds=config.request_delay_seconds,
            retry_attempts=config.retry_attempts,
            retry_backoff_seconds=config.retry_backoff_seconds,
            respect_robots=respect_robots,
        )

    def discover_chapters(self) -> tuple[NovelMetadata, list[ChapterLink]]:
        config = self.config
        toc_url = config.start_url
        start_netloc = urlparse(config.start_url).netloc
        visited_toc_urls: set[str] = set()
        seen_chapters: set[str] = set()
        chapters: list[ChapterLink] = []
        metadata: NovelMetadata | None = None

        for _ in range(config.max_toc_pages):
            if toc_url in visited_toc_urls:
                break
            visited_toc_urls.add(toc_url)

            response = self.client.fetch(toc_url)
            soup = BeautifulSoup(response.body, "html.parser")
            if metadata is None:
                metadata = self._extract_metadata(soup, response.url)

            for anchor in soup.select(config.chapter_link_selector):
                href = anchor.get("href")
                if not isinstance(href, str) or not href:
                    continue
                chapter_url = urljoin(response.url, href)
                if config.same_domain and urlparse(chapter_url).netloc != start_netloc:
                    continue
                if chapter_url in seen_chapters:
                    continue
                title = normalize_text(anchor.get_text(" ", strip=True)) or chapter_url
                chapters.append(ChapterLink(title=title, url=chapter_url))
                seen_chapters.add(chapter_url)

            next_url = self._next_toc_url(soup, response.url)
            if not next_url:
                break
            if config.same_domain and urlparse(next_url).netloc != start_netloc:
                break
            toc_url = next_url

        if config.reverse_chapter_order:
            chapters.reverse()
        if metadata is None:
            metadata = NovelMetadata(
                title=config.name,
                author=None,
                source_url=config.start_url,
                site_name=config.name,
            )
        return metadata, chapters

    def crawl(
        self,
        output_root: Path,
        *,
        max_chapters: int | None = None,
        fail_fast: bool = False,
        overwrite: bool = False,
        share_root: Path | None,
        progress_callback: ProgressCallback | None = None,
        workers: int = 1,
    ) -> CrawlResult:
        metadata, chapter_links = self.discover_chapters()
        if not chapter_links:
            raise FetchError("No chapter links found. Check chapter_link_selector.")

        novel_slug = slugify(metadata.title, fallback=slugify(self.config.name))
        novel_dir = output_root / novel_slug
        if share_root:
            chapter_output_dir = share_root / novel_slug / "input"
        else:
            chapter_output_dir = novel_dir / "chapters"
        novel_dir.mkdir(parents=True, exist_ok=True)
        chapter_output_dir.mkdir(parents=True, exist_ok=True)

        results: list[ChapterResult] = []
        errors: list[CrawlError] = []
        generated_at = datetime.now(UTC).isoformat()
        fetched_count = 0

        self._write_metadata(novel_dir / "metadata.json", metadata)
        self._write_json(novel_dir / "config.json", asdict(self.config))
        self._write_manifest(
            novel_dir / "manifest.json",
            generated_at=generated_at,
            status="running",
            metadata=metadata,
            runtime_output_dir=novel_dir,
            chapter_output_dir=chapter_output_dir,
            chapter_links=chapter_links,
            results=results,
            errors=errors,
        )

        def _write_running_manifest(*, status: str = "running") -> None:
            self._write_manifest(
                novel_dir / "manifest.json",
                generated_at=generated_at,
                status=status,
                metadata=metadata,
                runtime_output_dir=novel_dir,
                chapter_output_dir=chapter_output_dir,
                chapter_links=chapter_links,
                results=results,
                errors=errors,
            )

        def _fetch_chapter(
            index: int, chapter_link: ChapterLink, chapter_path: Path
        ) -> ChapterResult:
            self._report_progress(
                progress_callback,
                current=index,
                total=len(chapter_links),
                status="started",
                title=chapter_link.title,
                source_url=chapter_link.url,
                path=str(chapter_path),
            )
            title, body, final_url = self._fetch_chapter(chapter_link)
            self._write_text_atomic(chapter_path, self._chapter_text(title, body))
            return ChapterResult(
                index=index,
                title=title,
                source_url=final_url,
                path=str(chapter_path),
            )

        if workers < 1:
            raise ValueError("Number of workers must be at least 1.")

        # A strict fail-fast crawl cannot start speculative requests because an
        # in-flight HTTP request cannot be cancelled reliably.
        effective_workers = 1 if fail_fast else workers
        next_chapter = 0
        pending: dict[Future[ChapterResult], tuple[int, ChapterLink]] = {}

        def _fill_pending(executor: ThreadPoolExecutor) -> None:
            nonlocal next_chapter
            while next_chapter < len(chapter_links):
                if len(pending) >= effective_workers:
                    return
                if max_chapters is not None and fetched_count + len(pending) >= max_chapters:
                    return

                index = next_chapter + 1
                chapter_link = chapter_links[next_chapter]
                next_chapter += 1
                chapter_path = chapter_output_dir / f"chapter_{index}.txt"

                if not overwrite and self._is_existing_chapter(chapter_path):
                    results.append(
                        ChapterResult(
                            index=index,
                            title=chapter_link.title,
                            source_url=chapter_link.url,
                            path=str(chapter_path),
                            skipped=True,
                        )
                    )
                    self._report_progress(
                        progress_callback,
                        current=index,
                        total=len(chapter_links),
                        status="skipped",
                        title=chapter_link.title,
                        source_url=chapter_link.url,
                        path=str(chapter_path),
                    )
                    _write_running_manifest()
                    continue

                future = executor.submit(_fetch_chapter, index, chapter_link, chapter_path)
                pending[future] = (index, chapter_link)

        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            _fill_pending(executor)
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    index, chapter_link = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:
                        error_text = str(error)
                        errors.append(
                            {
                                "index": index,
                                "url": chapter_link.url,
                                "error": error_text,
                            }
                        )
                        self._report_progress(
                            progress_callback,
                            current=index,
                            total=len(chapter_links),
                            status="failed",
                            title=chapter_link.title,
                            source_url=chapter_link.url,
                            error=error_text,
                        )
                        _write_running_manifest(status="failed" if fail_fast else "running")
                        if fail_fast:
                            raise
                    else:
                        results.append(result)
                        fetched_count += 1
                        self._report_progress(
                            progress_callback,
                            current=index,
                            total=len(chapter_links),
                            status="fetched",
                            title=result.title,
                            source_url=result.source_url,
                            path=result.path,
                        )
                        _write_running_manifest()
                _fill_pending(executor)

        # Sort results by index so output is deterministic regardless of
        # parallel execution order.
        results.sort(key=lambda r: r.index)
        errors.sort(key=lambda error: error["index"])

        self._write_manifest(
            novel_dir / "manifest.json",
            generated_at=generated_at,
            status="completed",
            metadata=metadata,
            runtime_output_dir=novel_dir,
            chapter_output_dir=chapter_output_dir,
            chapter_links=chapter_links,
            results=results,
            errors=errors,
        )
        if share_root:
            self._write_metadata(chapter_output_dir.parent / "metadata.json", metadata)

        return CrawlResult(
            metadata=metadata,
            chapters=results,
            output_dir=str(novel_dir),
            chapter_output_dir=str(chapter_output_dir),
        )

    def _extract_metadata(self, soup: BeautifulSoup, source_url: str) -> NovelMetadata:
        title = self.config.name
        if self.config.novel_title_selector:
            title_node = soup.select_one(self.config.novel_title_selector)
            if title_node:
                title = normalize_text(title_node.get_text(" ", strip=True)) or title

        author = None
        if self.config.author_selector:
            author_node = soup.select_one(self.config.author_selector)
            if author_node:
                author = normalize_text(author_node.get_text(" ", strip=True)) or None

        return NovelMetadata(
            title=title,
            author=author,
            source_url=source_url,
            site_name=self.config.name,
        )

    def _next_toc_url(self, soup: BeautifulSoup, current_url: str) -> str | None:
        if not self.config.toc_next_selector:
            return None
        next_node = soup.select_one(self.config.toc_next_selector)
        if not next_node:
            return None
        href = next_node.get("href")
        if not isinstance(href, str) or not href:
            return None
        return urljoin(current_url, href)

    def _fetch_chapter(self, chapter_link: ChapterLink) -> tuple[str, str, str]:
        response = self.client.fetch(chapter_link.url)
        soup = BeautifulSoup(response.body, "html.parser")

        title = chapter_link.title
        if self.config.chapter_title_selector:
            title_node = soup.select_one(self.config.chapter_title_selector)
            if title_node:
                title = normalize_text(title_node.get_text(" ", strip=True)) or title

        for selector in self.config.remove_selectors:
            for node in soup.select(selector):
                node.decompose()

        content_node = soup.select_one(self.config.chapter_content_selector)
        if not content_node:
            raise FetchError(
                f"No chapter content found with selector: {self.config.chapter_content_selector}"
            )

        body = html_to_plain_text(content_node)
        if not body:
            raise FetchError("Chapter content was empty after cleanup.")
        return title, body, response.url

    @staticmethod
    def _write_json(path: Path, data: object) -> None:
        temp_path = path.with_suffix(path.suffix + ".tmp")
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)

    def _write_manifest(
        self,
        path: Path,
        *,
        generated_at: str,
        status: str,
        metadata: NovelMetadata,
        runtime_output_dir: Path,
        chapter_output_dir: Path,
        chapter_links: list[ChapterLink],
        results: list[ChapterResult],
        errors: list[CrawlError],
    ) -> None:
        skipped_count = sum(1 for result in results if result.skipped)
        manifest = {
            "generated_at": generated_at,
            "updated_at": datetime.now(UTC).isoformat(),
            "status": status,
            "config": asdict(self.config),
            "metadata": self._metadata_dict(metadata),
            "runtime_output_dir": str(runtime_output_dir),
            "chapter_output_dir": str(chapter_output_dir),
            "total_chapters": len(chapter_links),
            "completed_chapters": len(results) + len(errors),
            "fetched_chapters": len(results) - skipped_count,
            "skipped_chapters": skipped_count,
            "failed_chapters": len(errors),
            "discovered_chapters": [
                {"index": index, "title": chapter.title, "source_url": chapter.url}
                for index, chapter in enumerate(chapter_links, start=1)
            ],
            "chapters": [asdict(result) for result in results],
            "errors": errors,
        }
        self._write_json(path, manifest)

    @staticmethod
    def _metadata_dict(metadata: NovelMetadata) -> dict[str, object]:
        return {
            "title": metadata.title,
            "translated": metadata.translated,
            "author": metadata.author,
            "source_url": metadata.source_url,
            "illustration_url": metadata.illustration_url,
            "site_name": metadata.site_name,
        }

    def _write_metadata(self, path: Path, metadata: NovelMetadata) -> None:
        self._write_json(path, self._metadata_dict(metadata))

    @staticmethod
    def _chapter_text(title: str, body: str) -> str:
        return f"{normalize_text(title)}\n\n{body.strip()}\n"

    @staticmethod
    def _is_existing_chapter(path: Path) -> bool:
        return path.is_file() and path.stat().st_size > 0

    @staticmethod
    def _write_text_atomic(path: Path, text: str) -> None:
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)

    @staticmethod
    def _report_progress(
        progress_callback: ProgressCallback | None,
        *,
        current: int,
        total: int,
        status: str,
        title: str,
        source_url: str,
        path: str | None = None,
        error: str | None = None,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(
            CrawlProgress(
                current=current,
                total=total,
                status=status,
                title=title,
                source_url=source_url,
                path=path,
                error=error,
            )
        )
