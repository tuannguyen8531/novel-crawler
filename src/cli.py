from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.config import SiteConfig, config
from src.models import CrawlProgress
from src.services.browser import BrowserFetcher
from src.services.crawler import NovelCrawler
from src.services.http import FetchError

RUNTIME_OUTPUT_ROOT = Path("data")
CONFIG_DIR = Path("configs")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="novel-crawler",
        description="Download chapters from public novel websites using a per-site JSON config.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl = subparsers.add_parser("crawl", help="Download a novel into text files.")
    _add_crawl_arguments(crawl, target_help="Config path or novel name from configs/{novel}.json.")

    return parser


def build_short_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl",
        description="Download chapters from public novel websites.",
    )
    _add_crawl_arguments(parser, target_help="Config path or novel name from configs/{novel}.json.")
    return parser


def _add_crawl_arguments(parser: argparse.ArgumentParser, *, target_help: str) -> None:
    parser.add_argument("target", type=str, help=target_help)
    parser.add_argument(
        "--share-output",
        type=Path,
        default=None,
        help="Shared chapter output root. Default: NOVEL_SHARE_DIR or ../share",
    )
    parser.add_argument(
        "-m",
        "--max",
        "--max-chapters",
        type=int,
        default=None,
        dest="max_chapters",
        help="Stop after fetching this many new chapters. Default: MAX_CHAPTERS env or unlimited.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on the first chapter error instead of writing partial output.",
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Do not check robots.txt. Use only when you have permission.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only discover chapter links and print a preview.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download chapter files even if the shared chapter_N.txt already exists.",
    )
    parser.add_argument(
        "-b",
        "--browser",
        action="store_true",
        default=None,
        help="Use headless browser for JS challenges. Default: USE_BROWSER env.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "crawl":
        return _crawl(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


def crawl_main(argv: list[str] | None = None) -> int:
    parser = build_short_parser()
    args = parser.parse_args(argv)
    return _crawl(args)


def _crawl(args: argparse.Namespace) -> int:
    try:
        config_path = _resolve_config_path(args.target)
        site_config = SiteConfig.from_file(config_path)

        use_browser = args.browser if args.browser is not None else config.use_browser
        max_chapters = args.max_chapters if args.max_chapters is not None else None
        if max_chapters is None and config.max_chapters > 0:
            max_chapters = config.max_chapters

        share_root = args.share_output or config.share_path

        if use_browser:
            with BrowserFetcher(
                user_agent=site_config.user_agent,
                timeout_seconds=site_config.timeout_seconds,
                delay_seconds=site_config.request_delay_seconds,
                retry_attempts=site_config.retry_attempts,
                retry_backoff_seconds=site_config.retry_backoff_seconds,
            ) as fetcher:
                return _crawl_with_fetcher(site_config, fetcher, args, max_chapters, share_root)
        else:
            crawler = NovelCrawler(site_config, respect_robots=not args.ignore_robots)
            return _run_crawl(crawler, args, max_chapters, share_root)
    except (OSError, ValueError, FetchError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def _crawl_with_fetcher(
    site_config: SiteConfig, fetcher: object, args: argparse.Namespace,
    max_chapters: int | None, share_root: Path | None,
) -> int:
    crawler = NovelCrawler(
        site_config,
        respect_robots=not args.ignore_robots,
        fetcher=fetcher,  # type: ignore[arg-type]
    )
    return _run_crawl(crawler, args, max_chapters, share_root)


def _run_crawl(
    crawler: NovelCrawler, args: argparse.Namespace,
    max_chapters: int | None, share_root: Path | None,
) -> int:
    try:
        if args.dry_run:
            metadata, chapters = crawler.discover_chapters()
            if max_chapters is not None:
                chapters = chapters[:max_chapters]
            print(f"Title: {metadata.title}")
            if metadata.author:
                print(f"Author: {metadata.author}")
            print(f"Chapters found: {len(chapters)}")
            for index, chapter in enumerate(chapters[:10], start=1):
                print(f"{index:04d}. {chapter.title} - {chapter.url}")
            if len(chapters) > 10:
                print(f"... {len(chapters) - 10} more")
            return 0

        result = crawler.crawl(
            RUNTIME_OUTPUT_ROOT,
            max_chapters=max_chapters,
            fail_fast=args.fail_fast,
            overwrite=args.overwrite,
            share_root=share_root,
            progress_callback=_print_progress,
        )
    except (OSError, ValueError, FetchError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    skipped = sum(1 for ch in result.chapters if ch.skipped)
    fetched = len(result.chapters) - skipped
    print(f"Done: {result.metadata.title} ({fetched} new, {skipped} skipped)")
    return 0


def _resolve_config_path(target: str) -> Path:
    path = Path(target)
    if path.is_file():
        return path

    candidates = []
    if path.suffix == ".json":
        candidates.append(CONFIG_DIR / path)
    else:
        candidates.append(CONFIG_DIR / f"{target}.json")
        candidates.append(CONFIG_DIR / target / "config.json")

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    checked = ", ".join(str(candidate) for candidate in candidates)
    raise ValueError(f"Config not found for '{target}'. Checked: {checked}")


def _print_progress(progress: CrawlProgress) -> None:
    if progress.status in ("started", "skipped"):
        return
    if progress.status == "fetched":
        print(f"[{progress.current}/{progress.total}] {progress.title}", flush=True)
        return
    if progress.status == "failed":
        detail = progress.error or "unknown error"
        print(
            f"[{progress.current}/{progress.total}] {progress.title} (fail: {detail})",
            file=sys.stderr,
            flush=True,
        )
        return

    print(
        f"[{progress.current}/{progress.total}] {progress.title} ({progress.status})",
        flush=True,
    )
