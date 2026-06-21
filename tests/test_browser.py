from __future__ import annotations

import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from playwright.async_api import Error as PlaywrightError

from src.services.browser import BrowserFetcher


class FakeResponse:
    status = 200


class FakePage:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.url = ""
        self.closed = False

    async def goto(self, url: str, *, wait_until: str) -> FakeResponse:
        self.url = url
        with self.context.lock:
            self.context.active_pages += 1
            self.context.max_active_pages = max(
                self.context.max_active_pages,
                self.context.active_pages,
            )
        await asyncio.sleep(0.02)
        with self.context.lock:
            self.context.active_pages -= 1
        return FakeResponse()

    async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        return None

    async def content(self) -> str:
        return f"<html>{self.url}</html>"

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.active_pages = 0
        self.max_active_pages = 0
        self.lock = threading.Lock()
        self.closed = False
        self.timeout_ms: int | None = None

    async def new_page(self) -> FakePage:
        page = FakePage(self)
        self.pages.append(page)
        return page

    def set_default_timeout(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.context = FakeContext()
        self.closed = False

    async def new_context(self, **kwargs: object) -> FakeContext:
        return self.context

    async def close(self) -> None:
        self.closed = True


class CloseFailingBrowser(FakeBrowser):
    async def close(self) -> None:
        raise RuntimeError("Connection closed while reading from the driver")


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser

    async def launch(self, **kwargs: object) -> FakeBrowser:
        return self.browser


class FallbackChromium(FakeChromium):
    def __init__(self, browser: FakeBrowser) -> None:
        super().__init__(browser)
        self.launch_kwargs: list[dict[str, object]] = []

    async def launch(self, **kwargs: object) -> FakeBrowser:
        self.launch_kwargs.append(kwargs)
        if len(self.launch_kwargs) == 1:
            raise PlaywrightError("Bundled Chromium is unavailable.")
        return self.browser


class FakePlaywright:
    def __init__(self) -> None:
        self.browser = FakeBrowser()
        self.chromium = FakeChromium(self.browser)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakePlaywrightStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> FakePlaywright:
        return self.playwright


class BrowserFetcherTest(unittest.TestCase):
    def test_fetch_uses_shared_context_with_concurrent_pages(self) -> None:
        playwright = FakePlaywright()
        starter = FakePlaywrightStarter(playwright)
        urls = [
            "https://example.test/c1",
            "https://example.test/c2",
            "https://example.test/c3",
        ]

        with (
            patch("src.services.browser.async_playwright", return_value=starter),
            BrowserFetcher(
                user_agent="test",
                delay_seconds=0,
                max_concurrency=2,
            ) as fetcher,
            ThreadPoolExecutor(max_workers=3) as executor,
        ):
            responses = list(executor.map(fetcher.fetch, urls))

        context = playwright.browser.context
        self.assertEqual([response.url for response in responses], urls)
        self.assertEqual(context.max_active_pages, 2)
        self.assertEqual(len(context.pages), 3)
        self.assertTrue(all(page.closed for page in context.pages))
        self.assertTrue(context.closed)
        self.assertTrue(playwright.browser.closed)
        self.assertTrue(playwright.stopped)

    def test_rejects_zero_concurrency(self) -> None:
        with self.assertRaises(ValueError):
            BrowserFetcher(user_agent="test", max_concurrency=0).__enter__()

    def test_uses_system_browser_when_bundle_is_unavailable(self) -> None:
        playwright = FakePlaywright()
        chromium = FallbackChromium(playwright.browser)
        playwright.chromium = chromium
        starter = FakePlaywrightStarter(playwright)

        with (
            patch("src.services.browser.async_playwright", return_value=starter),
            patch(
                "src.services.browser._find_system_browser",
                return_value="/usr/bin/google-chrome",
            ),
            BrowserFetcher(user_agent="test", delay_seconds=0),
        ):
            pass

        self.assertEqual(len(chromium.launch_kwargs), 2)
        self.assertNotIn("executable_path", chromium.launch_kwargs[0])
        self.assertEqual(
            chromium.launch_kwargs[1]["executable_path"],
            "/usr/bin/google-chrome",
        )

    def test_close_error_does_not_mask_keyboard_interrupt(self) -> None:
        playwright = FakePlaywright()
        playwright.browser = CloseFailingBrowser()
        playwright.chromium = FakeChromium(playwright.browser)
        starter = FakePlaywrightStarter(playwright)

        with self.assertRaises(KeyboardInterrupt):
            with (
                patch("src.services.browser.async_playwright", return_value=starter),
                BrowserFetcher(user_agent="test", delay_seconds=0),
            ):
                raise KeyboardInterrupt

        self.assertTrue(playwright.browser.context.closed)
        self.assertTrue(playwright.stopped)


if __name__ == "__main__":
    unittest.main()
