from __future__ import annotations

import time
from dataclasses import dataclass, field

from playwright.sync_api import Page, Playwright, sync_playwright

from src.services.http import FetchError, FetchResponse


@dataclass
class BrowserFetcher:
    user_agent: str
    timeout_seconds: float = 30.0
    delay_seconds: float = 1.0
    retry_attempts: int = 3
    retry_backoff_seconds: float = 2.0
    _last_request_at: float = field(default=0.0, init=False)
    _pw: Playwright | None = field(default=None, init=False)
    _page: Page | None = field(default=None, init=False)

    def __enter__(self) -> BrowserFetcher:
        self._start()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _start(self) -> None:
        if self._pw is not None:
            return
        self._pw = sync_playwright().start()
        browser = self._pw.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        context.set_default_timeout(int(self.timeout_seconds * 1000))
        self._page = context.new_page()

    def close(self) -> None:
        self._pw = None
        self._page = None

    def fetch(self, url: str) -> FetchResponse:
        if self._page is None:
            self._start()

        page = self._page
        assert page is not None

        attempts = max(1, self.retry_attempts)
        final_url = url
        body = ""
        content_type = "text/html"
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                response = page.goto(url, wait_until="domcontentloaded")
                if response is None:
                    raise FetchError(f"No response from page: {url}")

                status = response.status
                if status >= 400:
                    if attempt < attempts:
                        self._retry_sleep(attempt)
                        continue
                    raise FetchError(
                        f"HTTP {status} while fetching {url}"
                    )

                page.wait_for_load_state("domcontentloaded", timeout=5000)
                body = page.content()
                final_url = page.url
                break
            except FetchError:
                raise
            except Exception as error:
                if attempt == attempts:
                    raise FetchError(
                        f"Browser error while fetching {url}: {error}"
                    ) from error
                self._retry_sleep(attempt)

        return FetchResponse(
            url=final_url,
            body=body,
            content_type=content_type,
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _retry_sleep(self, attempt: int) -> None:
        delay = self.retry_backoff_seconds * attempt
        if delay > 0:
            time.sleep(delay)
