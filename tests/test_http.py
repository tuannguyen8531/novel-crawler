from __future__ import annotations

import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from src.services.http import HttpClient


class HttpClientTest(unittest.TestCase):
    def test_fetch_retries_temporary_network_errors(self) -> None:
        response = Mock()
        response.headers.get.return_value = "text/html"
        response.headers.get_content_type.return_value = "text/html"
        response.headers.get_param.return_value = None
        response.read.return_value = b"<html><body>ok</body></html>"
        response.geturl.return_value = "https://example.test/page"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)

        opener = Mock()
        opener.open = Mock(side_effect=[URLError("temporary"), response])

        with (
            patch(
                "src.services.http.build_opener",
                return_value=opener,
            ),
            patch("src.services.http.time.sleep"),
        ):
            client = HttpClient(
                user_agent="test",
                delay_seconds=0,
                retry_attempts=2,
                respect_robots=False,
            )
            result = client.fetch("https://example.test/page")

        self.assertEqual(result.body, "<html><body>ok</body></html>")
        self.assertEqual(opener.open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
