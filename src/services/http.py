from __future__ import annotations

import re
import ssl
import time
from dataclasses import dataclass, field
from email.message import Message
from http.cookiejar import CookieJar
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener


class FetchError(RuntimeError):
    pass


_RETRIABLE_HTTP = {403, 429, 500, 502, 503, 504}

_BROWSER_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "DNT": "1",
}


@dataclass
class FetchResponse:
    url: str
    body: str
    content_type: str | None


@dataclass
class HttpClient:
    user_agent: str
    timeout_seconds: float = 30.0
    delay_seconds: float = 1.0
    retry_attempts: int = 3
    retry_backoff_seconds: float = 2.0
    respect_robots: bool = True
    _last_request_at: float = field(default=0.0, init=False)
    _robots: dict[str, robotparser.RobotFileParser] = field(default_factory=dict, init=False)
    _cookie_jar: CookieJar = field(default_factory=CookieJar, init=False)
    _last_url: str | None = field(default=None, init=False)

    def fetch(self, url: str) -> FetchResponse:
        if self.respect_robots and not self.can_fetch(url):
            raise FetchError(f"Blocked by robots.txt: {url}")

        attempts = max(1, self.retry_attempts)
        headers: Message | None = None
        raw_body = b""
        final_url = url
        for attempt in range(1, attempts + 1):
            self._throttle()
            request = self._build_request(url)
            try:
                with self._open(request) as response:
                    headers = response.headers
                    raw_body = response.read()
                    final_url = response.geturl()
                self._last_url = url
                break
            except HTTPError as error:
                if error.code in _RETRIABLE_HTTP and attempt < attempts:
                    self._retry_sleep(attempt)
                    continue
                raise FetchError(
                    f"HTTP {error.code} while fetching {url}"
                ) from error
            except URLError as error:
                if attempt == attempts:
                    reason = getattr(error, "reason", error)
                    raise FetchError(
                        f"Network error while fetching {url} after {attempts} attempts: {reason}"
                    ) from error
                self._retry_sleep(attempt)
            except (TimeoutError, ssl.SSLError) as error:
                if attempt == attempts:
                    raise FetchError(
                        f"Network error while fetching {url} after {attempts} attempts: {error}"
                    ) from error
                self._retry_sleep(attempt)

        encoding = _detect_encoding(headers, raw_body)
        return FetchResponse(
            url=final_url,
            body=raw_body.decode(encoding, errors="replace"),
            content_type=headers.get("Content-Type") if headers else None,
        )

    def can_fetch(self, url: str) -> bool:
        parser = self._robots_parser(url)
        return parser.can_fetch(self.user_agent, url)

    def _build_request(self, url: str) -> Request:
        hdrs = {"User-Agent": self.user_agent, **_BROWSER_HEADERS}
        parsed = urlparse(url)
        hdrs["Host"] = parsed.netloc
        if self._last_url:
            hdrs["Referer"] = self._last_url
        return Request(url, headers=hdrs)

    def _open(self, request: Request):
        opener = build_opener(HTTPCookieProcessor(self._cookie_jar))
        return opener.open(request, timeout=self.timeout_seconds)

    def _robots_parser(self, url: str) -> robotparser.RobotFileParser:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._robots:
            return self._robots[origin]

        robots_url = urljoin(origin, "/robots.txt")
        parser = robotparser.RobotFileParser(robots_url)
        parser.set_url(robots_url)
        try:
            request = self._build_request(robots_url)
            with self._open(request) as response:
                body = response.read().decode("utf-8", errors="replace")
            parser.parse(body.splitlines())
        except Exception:
            parser.parse([])
        self._robots[origin] = parser
        return parser

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


def _detect_encoding(headers: Message | None, body: bytes) -> str:
    if headers is None:
        return "utf-8"
    content_type = headers.get_content_type()
    charset = headers.get_param("charset")
    if isinstance(charset, str) and charset:
        return charset
    if content_type == "text/html":
        head = body[:4096].decode("ascii", errors="ignore")
        meta_match = re.search(r"<meta[^>]+charset=[\"']?\s*([a-zA-Z0-9_-]+)", head, re.I)
        if meta_match:
            return meta_match.group(1)
        return "utf-8"
    return "utf-8"
