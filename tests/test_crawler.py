from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.config import SiteConfig
from src.models import ChapterLink, CrawlProgress, NovelMetadata
from src.services.crawler import NovelCrawler
from src.services.http import FetchError, FetchResponse


class FakeClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.fetched_urls: list[str] = []

    def fetch(self, url: str) -> FetchResponse:
        self.fetched_urls.append(url)
        return FetchResponse(url=url, body=self.pages[url], content_type="text/html")


class FlakyClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def fetch(self, url: str) -> FetchResponse:
        with self.lock:
            self.calls.append(url)
        if "c1" in url:
            raise FetchError("Flaky fail")
        return FetchResponse(
            url=url,
            body="<h1>Chapter</h1><div class='content'>Succeed</div>",
            content_type="text/html",
        )


class BlockingFlakyClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def fetch(self, url: str) -> FetchResponse:
        with self.lock:
            self.calls.append(url)
        if "c1" in url:
            raise FetchError("Fail fast trigger")
        time.sleep(0.5)
        return FetchResponse(
            url=url,
            body="<h1>C</h1><div class='content'>Body</div>",
            content_type="text/html",
        )


class SuccessfulClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchResponse:
        self.calls.append(url)
        return FetchResponse(
            url=url,
            body="<h1>Chapter</h1><div class='content'>Succeed</div>",
            content_type="text/html",
        )


class DelayedExistingCheckCrawler(NovelCrawler):
    @staticmethod
    def _is_existing_chapter(path: Path) -> bool:
        if path.name == "chapter_1.txt":
            time.sleep(0.05)
        return False


def demo_config() -> SiteConfig:
    return SiteConfig.from_dict(
        {
            "name": "demo",
            "start_url": "https://public.example/novel",
            "novel_title_selector": "h1.title",
            "author_selector": ".author",
            "chapter_link_selector": ".chapters a",
            "chapter_title_selector": "h1",
            "chapter_content_selector": ".content",
            "remove_selectors": [".ads"],
            "request_delay_seconds": 0,
        }
    )


def demo_pages() -> dict[str, str]:
    return {
        "https://public.example/novel": """
            <h1 class="title">Demo Novel</h1>
            <span class="author">Demo Author</span>
            <nav class="chapters">
              <a href="/c1">Chapter 1</a>
              <a href="/c2">Chapter 2</a>
            </nav>
        """,
        "https://public.example/c1": """
            <h1>Chapter 1: Start</h1>
            <article class="content">
              <p>Hello world.</p>
              <p class="ads">Buy now.</p>
            </article>
        """,
        "https://public.example/c2": """
            <h1>Chapter 2: Next</h1>
            <article class="content"><p>Second chapter.</p></article>
        """,
    }


class NovelCrawlerTest(unittest.TestCase):
    def test_crawl_writes_metadata_and_shared_chapter_text(self) -> None:
        crawler = NovelCrawler(demo_config())
        crawler.client = FakeClient(demo_pages())  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as output:
            output_path = Path(output)
            result = crawler.crawl(output_path / "runtime", share_root=output_path / "share")
            novel_dir = Path(result.output_dir)
            chapter_dir = Path(result.chapter_output_dir)
            config_snapshot = novel_dir / "config.json"
            chapter_one = (chapter_dir / "chapter_1.txt").read_text(
                encoding="utf-8"
            )
            config_snapshot_exists = config_snapshot.is_file()

        self.assertEqual(result.metadata.title, "Demo Novel")
        self.assertEqual(result.metadata.author, "Demo Author")
        self.assertEqual(len(result.chapters), 2)
        self.assertTrue(result.chapters[0].path.endswith("demo-novel/input/chapter_1.txt"))
        self.assertFalse(result.chapters[0].skipped)
        self.assertTrue(config_snapshot_exists)
        self.assertTrue(chapter_one.startswith("Chapter 1: Start\n\n"))
        self.assertIn("Hello world.", chapter_one)
        self.assertNotIn("Buy now.", chapter_one)

    def test_crawl_skips_existing_chapter_files_by_default(self) -> None:
        fake_client = FakeClient(demo_pages())
        crawler = NovelCrawler(demo_config())
        crawler.client = fake_client  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as output:
            output_path = Path(output)
            first_result = crawler.crawl(
                output_path / "runtime",
                max_chapters=1,
                share_root=output_path / "share",
            )
            fake_client.fetched_urls.clear()
            second_result = crawler.crawl(
                output_path / "runtime",
                max_chapters=1,
                share_root=output_path / "share",
            )

            chapter_one = Path(first_result.chapter_output_dir) / "chapter_1.txt"
            chapter_two = Path(second_result.chapter_output_dir) / "chapter_2.txt"

            self.assertTrue(chapter_one.is_file())
            self.assertTrue(chapter_two.is_file())
            self.assertTrue(second_result.chapters[0].skipped)
            self.assertFalse(second_result.chapters[1].skipped)
            self.assertNotIn("https://public.example/c1", fake_client.fetched_urls)
            self.assertIn("https://public.example/c2", fake_client.fetched_urls)

    def test_crawl_overwrites_existing_chapter_files_when_requested(self) -> None:
        fake_client = FakeClient(demo_pages())
        crawler = NovelCrawler(demo_config())
        crawler.client = fake_client  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as output:
            output_path = Path(output)
            crawler.crawl(
                output_path / "runtime",
                max_chapters=1,
                share_root=output_path / "share",
            )
            fake_client.fetched_urls.clear()
            second_result = crawler.crawl(
                output_path / "runtime",
                max_chapters=1,
                overwrite=True,
                share_root=output_path / "share",
            )

        self.assertFalse(second_result.chapters[0].skipped)
        self.assertIn("https://public.example/c1", fake_client.fetched_urls)

    def test_crawl_reports_progress_and_updates_manifest_incrementally(self) -> None:
        crawler = NovelCrawler(demo_config())
        crawler.client = FakeClient(demo_pages())  # type: ignore[arg-type]
        progress_events: list[CrawlProgress] = []
        manifest_snapshots: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as output:
            output_path = Path(output)

            def progress_callback(progress: CrawlProgress) -> None:
                progress_events.append(progress)
                manifest_path = output_path / "runtime" / "demo-novel" / "manifest.json"
                manifest_snapshots.append(json.loads(manifest_path.read_text(encoding="utf-8")))

            crawler.crawl(
                output_path / "runtime",
                share_root=output_path / "share",
                progress_callback=progress_callback,
            )

            final_manifest = json.loads(
                (output_path / "runtime" / "demo-novel" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(
            [event.status for event in progress_events],
            ["started", "fetched", "started", "fetched"],
        )
        self.assertEqual(
            [snapshot["completed_chapters"] for snapshot in manifest_snapshots],
            [0, 0, 1, 1],
        )
        self.assertEqual(final_manifest["status"], "completed")
        self.assertEqual(final_manifest["total_chapters"], 2)
        self.assertEqual(final_manifest["fetched_chapters"], 2)

    def test_crawl_parallel_respects_max_chapters_with_skips(self) -> None:
        fake_client = FakeClient({
            **demo_pages(),
            "https://public.example/c3": (
                "<h1>Chapter 3: Extra</h1>"
                '<article class="content"><p>Third chapter.</p></article>'
            ),
        })
        fake_client.pages["https://public.example/novel"] = """
            <h1 class="title">Demo Novel</h1>
            <span class="author">Demo Author</span>
            <nav class="chapters">
              <a href="/c1">Chapter 1</a>
              <a href="/c2">Chapter 2</a>
              <a href="/c3">Chapter 3</a>
            </nav>
        """
        crawler = NovelCrawler(demo_config())
        crawler.client = fake_client  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as output:
            output_path = Path(output)
            crawler.crawl(
                output_path / "runtime",
                max_chapters=1,
                share_root=output_path / "share",
                workers=2,
            )
            fake_client.fetched_urls.clear()
            progress_snapshots: list[tuple[str, int, int]] = []

            def progress_callback(progress: CrawlProgress) -> None:
                manifest_path = output_path / "runtime" / "demo-novel" / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                progress_snapshots.append(
                    (progress.status, progress.current, manifest["completed_chapters"])
                )

            second_result = crawler.crawl(
                output_path / "runtime",
                max_chapters=1,
                share_root=output_path / "share",
                progress_callback=progress_callback,
                workers=2,
            )

            chapter_one = Path(second_result.chapter_output_dir) / "chapter_1.txt"
            chapter_two = Path(second_result.chapter_output_dir) / "chapter_2.txt"
            chapter_three = Path(second_result.chapter_output_dir) / "chapter_3.txt"

            self.assertTrue(chapter_one.is_file())
            self.assertTrue(chapter_two.is_file())
            self.assertFalse(chapter_three.is_file())

            self.assertEqual(len(second_result.chapters), 2)
            self.assertTrue(second_result.chapters[0].skipped)
            self.assertFalse(second_result.chapters[1].skipped)

            self.assertNotIn("https://public.example/c1", fake_client.fetched_urls)
            self.assertIn("https://public.example/c2", fake_client.fetched_urls)
            self.assertNotIn("https://public.example/c3", fake_client.fetched_urls)
            self.assertIn(("started", 2, 1), progress_snapshots)

    def test_parallel_max_chapters_recovers_from_failures(self) -> None:
        crawler = NovelCrawler(demo_config())
        client = FlakyClient()
        crawler.client = client
        crawler.discover_chapters = lambda: (
            NovelMetadata(title="Flaky", author=None, source_url="url", site_name="flaky"),
            [
                ChapterLink(title="C1", url="https://public.example/c1"),
                ChapterLink(title="C2", url="https://public.example/c2"),
                ChapterLink(title="C3", url="https://public.example/c3"),
            ],
        )

        with tempfile.TemporaryDirectory() as output:
            result = crawler.crawl(
                Path(output) / "runtime",
                max_chapters=1,
                share_root=Path(output) / "share",
                workers=2,
            )

        self.assertEqual(len(result.chapters), 1)
        self.assertEqual(result.chapters[0].title, "Chapter")
        self.assertFalse(result.chapters[0].skipped)
        self.assertEqual(len(client.calls), 2)

    def test_fail_fast_halts_workers_immediately(self) -> None:
        crawler = NovelCrawler(demo_config())
        client = BlockingFlakyClient()
        crawler.client = client
        crawler.discover_chapters = lambda: (
            NovelMetadata(title="FailFast", author=None, source_url="url", site_name="failfast"),
            [
                ChapterLink(title="C1", url="https://public.example/c1"),
                ChapterLink(title="C2", url="https://public.example/c2"),
                ChapterLink(title="C3", url="https://public.example/c3"),
            ],
        )

        with tempfile.TemporaryDirectory() as output, self.assertRaises(FetchError):
            crawler.crawl(
                Path(output) / "runtime",
                fail_fast=True,
                share_root=Path(output) / "share",
                workers=3,
            )

        self.assertEqual(client.calls, ["https://public.example/c1"])

    def test_parallel_max_chapters_preserves_chapter_order(self) -> None:
        client = SuccessfulClient()
        crawler = DelayedExistingCheckCrawler(demo_config(), fetcher=client)
        crawler.discover_chapters = lambda: (
            NovelMetadata(title="Ordered", author=None, source_url="url", site_name="ordered"),
            [
                ChapterLink(title="C1", url="https://public.example/c1"),
                ChapterLink(title="C2", url="https://public.example/c2"),
                ChapterLink(title="C3", url="https://public.example/c3"),
            ],
        )

        with tempfile.TemporaryDirectory() as output:
            result = crawler.crawl(
                Path(output) / "runtime",
                max_chapters=1,
                share_root=None,
                workers=2,
            )

        self.assertEqual(client.calls, ["https://public.example/c1"])
        self.assertEqual([chapter.index for chapter in result.chapters], [1])


if __name__ == "__main__":
    unittest.main()
