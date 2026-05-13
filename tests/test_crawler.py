from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.config import SiteConfig
from src.models import CrawlProgress
from src.services.crawler import NovelCrawler
from src.services.http import FetchResponse


class FakeClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.fetched_urls: list[str] = []

    def fetch(self, url: str) -> FetchResponse:
        self.fetched_urls.append(url)
        return FetchResponse(url=url, body=self.pages[url], content_type="text/html")


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
        self.assertTrue(result.chapters[0].path.endswith("share/demo-novel/chapter_1.txt"))
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


if __name__ == "__main__":
    unittest.main()
