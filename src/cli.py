from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from src.config import SiteConfig, config
from src.models import CrawlProgress
from src.services.browser import BrowserFetcher
from src.services.config_generator import ConfigGenerator
from src.services.crawler import NovelCrawler
from src.services.epub_importer import EpubImportError, import_epub
from src.services.http import FetchError, HttpClient
from src.services.llm import get_llm
from src.utils.logging import get_logger, setup_logging

RUNTIME_OUTPUT_ROOT = Path("data")
CONFIG_DIR = Path("configs")
DEFAULT_SHARE_ROOT = Path("../share")
_quiet_output = False
_progress_printer: CrawlProgressPrinter | None = None


class CrawlProgressPrinter:
    _SPINNER = ("-", "\\", "|", "/")

    def __init__(self) -> None:
        self._last_line = ""
        self._spinner_index = 0
        self._lock = threading.Lock()

    def update(self, progress: CrawlProgress) -> None:
        with self._lock:
            spinner = self._SPINNER[self._spinner_index % len(self._SPINNER)]
            self._spinner_index += 1

            if progress.status == "started":
                label = "fetching"
            elif progress.status == "fetched":
                label = "last"
            elif progress.status == "skipped":
                label = "skipped"
            else:
                label = progress.status

            line = f"  {spinner} [{progress.current}/{progress.total}] {label}: {progress.title}"
            self._last_line = line

            sys.stdout.write(f"\r\033[K{line}")
            sys.stdout.flush()

    def finish(self) -> None:
        with self._lock:
            if not self._last_line:
                return
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._last_line = ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="novel-crawler",
        description="Download chapters from public novel websites using a per-site JSON config.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress crawler progress and non-error logs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl = subparsers.add_parser("crawl", help="Download a novel into text files.")
    _add_crawl_arguments(crawl, target_help="Config path or novel name from configs/{novel}.json.")

    gen = subparsers.add_parser("generate", help="Use AI to generate a site config from a TOC URL.")
    _add_generate_arguments(gen)

    validate = subparsers.add_parser(
        "validate",
        help="Test a config's selectors against live HTML.",
    )
    _add_validate_arguments(validate)

    import_parser = subparsers.add_parser(
        "import",
        help="Import an EPUB into shared translator input files.",
    )
    _add_import_arguments(import_parser)

    return parser


def build_short_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl",
        description="Download chapters from public novel websites.",
    )
    _add_crawl_arguments(parser, target_help="Config path or novel name from configs/{novel}.json.")
    return parser


def build_generate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate",
        description="Use AI to generate a site config from a TOC URL.",
    )
    _add_generate_arguments(parser)
    return parser


def build_validate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate",
        description="Test a config's selectors against live HTML.",
    )
    _add_validate_arguments(parser)
    return parser


def build_import_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="import",
        description="Import an EPUB into shared translator input files.",
    )
    _add_import_arguments(parser)
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
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help="Concurrent chapter downloads. Default: 1.",
    )


def _add_generate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("url", type=str, help="URL of the novel's table-of-contents page.")
    parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Config name (default: derived from URL).",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="LLM provider override (ollama/gemini).",
    )
    parser.add_argument(
        "-b",
        "--browser",
        action="store_true",
        help="Use headless browser to fetch pages.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Skip the HTML cache and always re-fetch pages.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=CONFIG_DIR,
        help=f"Output directory (default: {CONFIG_DIR}).",
    )


def _add_validate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        type=str,
        help="Config path or novel name from configs/{novel}.json.",
    )
    parser.add_argument(
        "-b",
        "--browser",
        action="store_true",
        help="Use headless browser to fetch pages.",
    )


def _add_import_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("epub", type=Path, help="EPUB file path to import.")
    parser.add_argument(
        "-n",
        "--name",
        type=str,
        default=None,
        help="Output slug name. Defaults to the EPUB filename.",
    )
    parser.add_argument(
        "--share-output",
        type=Path,
        default=None,
        help="Shared output root. Default: NOVEL_SHARE_DIR or ../share.",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="Keep existing chapter_*.txt files in the target input directory.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    _setup_cli_logging(verbose=args.verbose, quiet=args.quiet)

    if args.command == "crawl":
        return _crawl(args)
    if args.command == "generate":
        return _generate(args)
    if args.command == "validate":
        return _validate(args)
    if args.command == "import":
        return _import_epub(args)

    parser.error(f"Unknown command: {args.command}")
    return 2


def crawl_main(argv: list[str] | None = None) -> int:
    parser = build_short_parser()
    args = parser.parse_args(argv)
    _setup_cli_logging()
    return _crawl(args)


def generate_main(argv: list[str] | None = None) -> int:
    parser = build_generate_parser()
    args = parser.parse_args(argv)
    _setup_cli_logging()
    return _generate(args)


def validate_main(argv: list[str] | None = None) -> int:
    parser = build_validate_parser()
    args = parser.parse_args(argv)
    _setup_cli_logging()
    return _validate(args)


def import_main(argv: list[str] | None = None) -> int:
    parser = build_import_parser()
    args = parser.parse_args(argv)
    _setup_cli_logging()
    return _import_epub(args)


def _setup_cli_logging(*, verbose: bool = False, quiet: bool = False) -> None:
    global _quiet_output, _progress_printer
    _quiet_output = quiet
    _progress_printer = CrawlProgressPrinter()
    log_level = "debug" if verbose else ("error" if quiet else "info")
    setup_logging(log_level)


def _print_output(*args: object, **kwargs: Any) -> None:
    if not _quiet_output:
        print(*args, **kwargs)


def _crawl(args: argparse.Namespace) -> int:
    try:
        config_path = _resolve_config_path(args.target)
        site_config = SiteConfig.from_file(config_path)

        use_browser = args.browser if args.browser is not None else config.use_browser
        if args.workers is None:
            args.workers = 1
        if args.workers < 1:
            raise ValueError("Number of workers must be at least 1.")

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
                max_concurrency=args.workers,
            ) as fetcher:
                return _crawl_with_fetcher(site_config, fetcher, args, max_chapters, share_root)
        else:
            crawler = NovelCrawler(site_config, respect_robots=not args.ignore_robots)
            return _run_crawl(crawler, args, max_chapters, share_root)
    except (OSError, ValueError, FetchError) as error:
        get_logger().error("Error: %s", error)
        return 1
    except KeyboardInterrupt:
        _finish_progress_line()
        get_logger().warning("Interrupted. Progress saved.")
        return 130


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
            _print_output(f"Title: {metadata.title}")
            if metadata.author:
                _print_output(f"Author: {metadata.author}")
            _print_output(f"Chapters found: {len(chapters)}")
            for index, chapter in enumerate(chapters[:10], start=1):
                _print_output(f"{index:04d}. {chapter.title} - {chapter.url}")
            if len(chapters) > 10:
                _print_output(f"... {len(chapters) - 10} more")
            return 0

        result = crawler.crawl(
            RUNTIME_OUTPUT_ROOT,
            max_chapters=max_chapters,
            fail_fast=args.fail_fast,
            overwrite=args.overwrite,
            share_root=share_root,
            progress_callback=_print_progress,
            workers=args.workers,
        )
    except (OSError, ValueError, FetchError) as error:
        _finish_progress_line()
        get_logger().error("Error: %s", error)
        return 1

    skipped = sum(1 for ch in result.chapters if ch.skipped)
    fetched = len(result.chapters) - skipped
    _finish_progress_line()
    _print_output(f"Done: {result.metadata.title} ({fetched} new, {skipped} skipped)")
    return 0


def _import_epub(args: argparse.Namespace) -> int:
    try:
        share_root = args.share_output or config.share_path or DEFAULT_SHARE_ROOT
        result = import_epub(
            args.epub,
            share_root,
            name=args.name,
            keep_existing=args.keep_existing,
        )
    except (OSError, ValueError, EpubImportError) as error:
        get_logger().error("Error: %s", error)
        return 1

    for warning in result.warnings:
        get_logger().warning(warning)
    _print_output(
        f"Imported: {result.metadata.title} "
        f"({len(result.chapters)} chapters, {len(result.illustrations)} illustrations)"
    )
    _print_output(f"Output: {result.output_dir}")
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
    if _quiet_output:
        return
    if progress.status in ("started", "fetched", "skipped"):
        printer = _progress_printer or CrawlProgressPrinter()
        printer.update(progress)
        return
    if progress.status == "failed":
        _finish_progress_line()
        detail = progress.error or "unknown error"
        print(
            f"[{progress.current}/{progress.total}] {progress.title} (fail: {detail})",
            file=sys.stderr,
            flush=True,
        )
        return

    printer = _progress_printer or CrawlProgressPrinter()
    printer.update(progress)


def _finish_progress_line() -> None:
    if _quiet_output:
        return
    if _progress_printer is not None:
        _progress_printer.finish()


def _fetch_toc_for_config(fetcher: object, site_config: SiteConfig) -> Any:
    if not site_config.toc_expand_selector:
        return fetcher.fetch(site_config.start_url)  # type: ignore[attr-defined]

    fetch_with_clicks = getattr(fetcher, "fetch_with_clicks", None)
    if fetch_with_clicks is None:
        raise FetchError("toc_expand_selector requires browser mode (-b/--browser).")
    return fetch_with_clicks(
        site_config.start_url,
        [site_config.toc_expand_selector],
        wait_for_selector=site_config.chapter_link_selector,
    )


def _generate(args: argparse.Namespace) -> int:
    """Generate a site config using AI."""
    try:
        # Use override provider if --provider was given.
        if args.provider:
            from src.services.llm.factory import _create_provider
            llm = _create_provider(args.provider)
        else:
            llm = get_llm()

        generator = ConfigGenerator(llm, use_browser=args.browser)
        cache_dir = None if args.no_cache else Path("data") / ".gen-cache"
        config_dict = generator.generate(args.url, name=args.name, cache_dir=cache_dir)

        # Validate before showing.
        try:
            ConfigGenerator.validate(config_dict)
        except ValueError as e:
            get_logger().warning("Validation warning: %s", e)

        # Show result for review.
        print(f"\n{'═' * 60}")
        print("Generated config:")
        print(f"{'═' * 60}")
        print(json.dumps(config_dict, ensure_ascii=False, indent=2))
        print(f"{'═' * 60}")

        # Ask for confirmation.
        output_dir: Path = args.output
        name = config_dict.get("name", "generated")
        dest = output_dir / f"{name}.json"
        answer = input(f"\nSave to {dest}? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            path = ConfigGenerator.save(config_dict, output_dir)
            print(f"✅ Config saved to {path}")
            return 0
        else:
            print("Cancelled.")
            return 0

    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as e:
        get_logger().error("Error: %s", e)
        return 1


def _validate(args: argparse.Namespace) -> int:
    """Test a config's selectors against live HTML."""
    try:
        config_path = _resolve_config_path(args.target)
        site_config = SiteConfig.from_file(config_path)

        use_browser = args.browser if args.browser is not None else config.use_browser

        if use_browser:
            browser_fetcher = BrowserFetcher(
                user_agent=site_config.user_agent,
                timeout_seconds=site_config.timeout_seconds,
                delay_seconds=site_config.request_delay_seconds,
            )
            browser_fetcher.__enter__()
            fetcher: BrowserFetcher | HttpClient = browser_fetcher
        else:
            fetcher = HttpClient(
                user_agent=site_config.user_agent,
                timeout_seconds=site_config.timeout_seconds,
                delay_seconds=site_config.request_delay_seconds,
                respect_robots=False,
            )

        try:
            print(f"\n{'═' * 60}")
            print("Validating config selectors")
            print(f"{'═' * 60}")
            print(f"Config: {config_path}")
            print(f"Start URL: {site_config.start_url}")
            print(f"Fetcher: {'browser' if use_browser else 'http'}")
            print()

            # --- TOC validation ---
            print("📖 TOC Page")
            print(f"   URL: {site_config.start_url}")
            toc_html = _fetch_toc_for_config(fetcher, site_config).body
            toc_soup = BeautifulSoup(toc_html, "html.parser")

            for label, selector in [
                ("novel_title_selector", site_config.novel_title_selector),
                ("author_selector", site_config.author_selector),
                ("chapter_link_selector", site_config.chapter_link_selector),
                ("toc_next_selector", site_config.toc_next_selector),
                ("toc_expand_selector", site_config.toc_expand_selector),
            ]:
                if selector:
                    matches = len(toc_soup.select(selector))
                    status = "✅" if matches > 0 else "❌"
                    print(f"   {status} {label}: '{selector}' → {matches} match(es)")
                else:
                    print(f"   ⏭  {label}: null (skipped)")

            # --- Chapter validation ---
            from src.services.crawler import NovelCrawler
            crawler = NovelCrawler(site_config, fetcher=fetcher)
            metadata, chapters = crawler.discover_chapters()

            print()
            print(f"📚 Discovered {len(chapters)} chapters")
            print(f"   Title: {metadata.title}")
            if metadata.author:
                print(f"   Author: {metadata.author}")

            if chapters:
                first = chapters[0]
                print()
                print("📄 Sample Chapter")
                print(f"   URL: {first.url}")
                ch_html = fetcher.fetch(first.url).body
                ch_soup = BeautifulSoup(ch_html, "html.parser")

                for label, selector in [
                    ("chapter_title_selector", site_config.chapter_title_selector),
                    ("chapter_content_selector", site_config.chapter_content_selector),
                ]:
                    if selector:
                        matches = len(ch_soup.select(selector))
                        status = "✅" if matches > 0 else "❌"
                        print(f"   {status} {label}: '{selector}' → {matches} match(es)")
                    else:
                        print(f"   ⏭  {label}: null (skipped)")

                if site_config.remove_selectors:
                    print("   remove_selectors:")
                    for sel in site_config.remove_selectors:
                        matches = len(ch_soup.select(sel))
                        status = "✅" if matches > 0 else "⚠️"
                        print(f"      {status} '{sel}' → {matches} match(es)")
                else:
                    print("   remove_selectors: [] (none configured)")

                # Test content extraction
                content_node = ch_soup.select_one(site_config.chapter_content_selector)
                if content_node:
                    text_len = len(content_node.get_text(strip=True))
                    print(f"   Extracted content length: {text_len} chars")
                    if text_len < 100:
                        print("   ⚠️  Content very short — check selectors or remove_selectors")
                else:
                    print(
                        "   ❌ Could not extract content — "
                        "chapter_content_selector returned 0 matches"
                    )

            print(f"\n{'═' * 60}")

        finally:
            if use_browser and isinstance(fetcher, BrowserFetcher):
                fetcher.__exit__(None, None, None)

        return 0

    except (OSError, ValueError, FetchError) as error:
        get_logger().error("Error: %s", error)
        return 1
