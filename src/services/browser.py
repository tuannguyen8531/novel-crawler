from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from shutil import which
from typing import Any, TypeVar

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from src.services.http import FetchError, FetchResponse
from src.utils.logging import get_logger

_T = TypeVar("_T")
_SYSTEM_BROWSER_COMMANDS = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
)


@dataclass
class BrowserFetcher:
    """Thread-safe sync facade over an async Playwright browser pool."""

    user_agent: str
    timeout_seconds: float = 30.0
    delay_seconds: float = 1.0
    retry_attempts: int = 3
    retry_backoff_seconds: float = 2.0
    max_concurrency: int = 1
    _last_request_at: float = field(default=0.0, init=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False)
    _loop_thread: threading.Thread | None = field(default=None, init=False)
    _start_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _pw: Playwright | None = field(default=None, init=False)
    _browser: Browser | None = field(default=None, init=False)
    _context: BrowserContext | None = field(default=None, init=False)
    _semaphore: asyncio.Semaphore | None = field(default=None, init=False)
    _throttle_lock: asyncio.Lock | None = field(default=None, init=False)

    def __enter__(self) -> BrowserFetcher:
        self._start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self.close(suppress_errors=exc_type is not None)

    def _start(self) -> None:
        with self._start_lock:
            if self._loop is not None:
                return
            if self.max_concurrency < 1:
                raise ValueError("Browser max_concurrency must be at least 1.")

            loop = asyncio.new_event_loop()
            loop_thread = threading.Thread(
                target=self._run_event_loop,
                args=(loop,),
                daemon=True,
                name="browser-fetcher",
            )
            self._loop = loop
            self._loop_thread = loop_thread
            loop_thread.start()
            try:
                self._submit(self._async_start())
            except Exception:
                with suppress(Exception):
                    self._submit(self._async_close())
                self._stop_event_loop()
                raise

    @staticmethod
    def _run_event_loop(loop: asyncio.AbstractEventLoop) -> None:
        def _heartbeat() -> None:
            if loop.is_running():
                # Keep thread-safe submissions responsive in restricted
                # environments where the selector wake-up pipe is unavailable.
                loop.call_later(0.1, _heartbeat)

        asyncio.set_event_loop(loop)
        loop.call_soon(_heartbeat)
        loop.run_forever()
        loop.close()

    async def _async_start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._launch_browser()
        self._context = await self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        self._context.set_default_timeout(int(self.timeout_seconds * 1000))
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._throttle_lock = asyncio.Lock()

    async def _launch_browser(self) -> Browser:
        assert self._pw is not None
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            return await self._pw.chromium.launch(args=launch_args)
        except PlaywrightError:
            executable_path = _find_system_browser()
            if executable_path is None:
                raise
            get_logger().warning(
                "Playwright Chromium is unavailable. Falling back to system browser: %s",
                executable_path,
            )
            return await self._pw.chromium.launch(
                executable_path=executable_path,
                args=launch_args,
            )

    def close(self, *, suppress_errors: bool = False) -> None:
        with self._start_lock:
            if self._loop is None:
                return
            try:
                try:
                    self._submit(self._async_close())
                except Exception as error:
                    if not suppress_errors:
                        raise
                    get_logger().debug(
                        "Ignoring browser close error during exception cleanup: %s",
                        error,
                    )
            finally:
                self._stop_event_loop()

    async def _async_close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            self._context = None
            try:
                if self._browser is not None:
                    await self._browser.close()
            finally:
                self._browser = None
                if self._pw is not None:
                    await self._pw.stop()
                self._pw = None
                self._semaphore = None
                self._throttle_lock = None

    def _stop_event_loop(self) -> None:
        loop = self._loop
        loop_thread = self._loop_thread
        self._loop = None
        self._loop_thread = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if loop_thread is not None:
            loop_thread.join()

    def _submit(self, coroutine: Coroutine[Any, Any, _T]) -> _T:
        loop = self._loop
        if loop is None:
            coroutine.close()
            raise RuntimeError("Browser fetcher is not running.")
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    def fetch(self, url: str) -> FetchResponse:
        self._start()
        return self._submit(self._fetch(url))

    async def _fetch(self, url: str) -> FetchResponse:
        context = self._context
        semaphore = self._semaphore
        if context is None or semaphore is None:
            raise RuntimeError("Browser fetcher is not running.")

        async with semaphore:
            page = await context.new_page()
            try:
                attempts = max(1, self.retry_attempts)
                final_url = url
                body = ""
                for attempt in range(1, attempts + 1):
                    await self._throttle()
                    try:
                        response = await page.goto(url, wait_until="domcontentloaded")
                        if response is None:
                            raise FetchError(f"No response from page: {url}")

                        status = response.status
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                        body = await page.content()
                        final_url = page.url

                        # Some sites return an error status but still render
                        # usable content via JavaScript.
                        if status >= 400:
                            has_content = body and len(body.strip()) > 500
                            if has_content:
                                break
                            if attempt < attempts:
                                await self._retry_sleep(attempt)
                                continue
                            raise FetchError(f"HTTP {status} while fetching {url}")

                        break
                    except FetchError:
                        raise
                    except Exception as error:
                        if attempt == attempts:
                            raise FetchError(
                                f"Browser error while fetching {url}: {error}"
                            ) from error
                        await self._retry_sleep(attempt)

                return FetchResponse(
                    url=final_url,
                    body=body,
                    content_type="text/html",
                )
            finally:
                await page.close()

    async def _throttle(self) -> None:
        throttle_lock = self._throttle_lock
        if throttle_lock is None:
            raise RuntimeError("Browser fetcher is not running.")

        async with throttle_lock:
            elapsed = time.monotonic() - self._last_request_at
            remaining = self.delay_seconds - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._last_request_at = time.monotonic()

    async def _retry_sleep(self, attempt: int) -> None:
        delay = self.retry_backoff_seconds * attempt
        if delay > 0:
            await asyncio.sleep(delay)


def _find_system_browser() -> str | None:
    for command in _SYSTEM_BROWSER_COMMANDS:
        executable_path = which(command)
        if executable_path is not None:
            return executable_path
    return None
