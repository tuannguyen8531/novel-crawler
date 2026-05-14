"""
LLM Service — Abstract base class for provider implementations.

All providers inherit from this class and implement `_do_generate()`.
Shared logic (HTTP client, retry, logging) lives here.
"""

import sys
import threading
import time
from abc import ABC, abstractmethod

import httpx

from src.config import config
from src.services.logger import log_api_request_received, log_api_request_sent, log_error


_SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧"


class _Spinner:
    """Simple terminal spinner running on a background thread."""

    def __init__(self, message: str):
        self._message = message
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        idx = 0
        while not self._stop.is_set():
            sys.stdout.write(f"\r  {_SPINNER_CHARS[idx]} {self._message}")
            sys.stdout.flush()
            idx = (idx + 1) % len(_SPINNER_CHARS)
            self._stop.wait(0.1)
        sys.stdout.write("\r" + " " * (len(self._message) + 4) + "\r")
        sys.stdout.flush()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        self._thread.join()


class BaseProvider(ABC):
    """Base class for LLM providers."""

    def __init__(
        self,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        self.temperature = temperature if temperature is not None else config.llm_temperature
        self.max_tokens = max_tokens or config.llm_max_tokens
        self._client: httpx.Client | None = None

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Provider identifier (e.g., 'ollama', 'gemini')."""
        ...

    @property
    def _client_instance(self) -> httpx.Client:
        """Lazy-init HTTP client."""
        if self._client is None:
            self._client = httpx.Client(timeout=300.0)
        return self._client

    def generate(self, system_prompt: str, user_prompt: str, call_type: str) -> str:
        """Generate text with auto-retry on rate limits."""
        max_retries = 3
        backoff_delays = [5, 10, 20]

        for attempt in range(max_retries + 1):
            try:
                with _Spinner(f"Calling {self.provider_name} ({call_type})..."):
                    return self._do_generate(system_prompt, user_prompt, call_type)
            except RuntimeError as e:
                error_msg = str(e)
                log_error(
                    context=f"LLM API Error (provider: {self.provider_name}, type: {call_type})",
                    error=e,
                    attempt=attempt + 1,
                    max_retries=max_retries
                )
                is_retryable = "429" in error_msg or "503" in error_msg or "500" in error_msg or "rate" in error_msg.lower() or "internal" in error_msg.lower()

                if is_retryable and attempt < max_retries:
                    delay = backoff_delays[attempt]
                    print(f"  Rate limited — waiting {delay}s before retry ({attempt + 1}/{max_retries})...")
                    time.sleep(delay)
                    continue
                raise

        raise RuntimeError(f"{self.provider_name} failed after {max_retries + 1} attempts")

    @abstractmethod
    def _do_generate(self, system_prompt: str, user_prompt: str, call_type: str) -> str:
        """Make the actual API call. Must be implemented by subclasses."""
        ...

    def _log_request_sent(
        self,
        call_type: str,
        url: str,
        request_body: dict,
    ) -> str:
        """Log the API request immediately when sent. Returns call_id."""
        return log_api_request_sent(
            call_type=call_type,
            provider=self.provider_name,
            url=url,
            request_body=request_body,
        )

    def _log_request_received(
        self,
        call_id: str,
        call_type: str,
        url: str,
        response_body: dict,
        status_code: int,
        duration_ms: float,
    ):
        """Log the API response after it arrives."""
        log_api_request_received(
            call_id=call_id,
            call_type=call_type,
            provider=self.provider_name,
            url=url,
            response_body=response_body,
            status_code=status_code,
            duration_ms=duration_ms,
        )

    def _check_response(self, response: httpx.Response):
        """Check HTTP response and raise a readable error if it failed."""
        if response.is_success:
            return

        try:
            error_data = response.json()
            error_msg = error_data.get("error", {}).get("message", "") or str(error_data)
        except Exception:
            error_msg = response.text[:500]

        raise RuntimeError(
            f"{self.provider_name} API error ({response.status_code}): {error_msg}"
        )

    def close(self):
        """Close the HTTP client."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
