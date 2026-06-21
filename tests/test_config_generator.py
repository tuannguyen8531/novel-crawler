from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.services.config_generator import ConfigGenerator, _HtmlCache


class ConfigGeneratorTest(unittest.TestCase):
    def test_load_known_domain_config_finds_match(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            configs_dir = Path(tempdir)
            (configs_dir / "known.json").write_text(
                '{"start_url": "https://example.com/book/1/", "chapter_link_selector": "a"}',
                encoding="utf-8",
            )
            result = ConfigGenerator._load_known_domain_config("example.com", configs_dir)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["chapter_link_selector"], "a")

    def test_load_known_domain_config_returns_none_for_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            configs_dir = Path(tempdir)
            (configs_dir / "known.json").write_text(
                '{"start_url": "https://example.com/book/1/"}',
                encoding="utf-8",
            )
            result = ConfigGenerator._load_known_domain_config("other.com", configs_dir)
            self.assertIsNone(result)

    def test_html_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            cache = _HtmlCache(Path(tempdir))
            html = (
                "<html><head><title>Real Page</title></head><body><p>"
                + "x" * 300
                + "</p></body></html>"
            )
            cache.set("https://example.com", html)
            self.assertEqual(cache.get("https://example.com"), html)
            self.assertIsNone(cache.get("https://other.com"))

    def test_html_cache_invalidates_bad_html(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            cache = _HtmlCache(Path(tempdir))
            cache.set("https://example.com", "<html></html>")
            self.assertIsNone(cache.get("https://example.com"))

    def test_build_config_includes_toc_expand_selector(self) -> None:
        result = ConfigGenerator._build_config(
            "https://example.com/book/1/",
            "example",
            {
                "novel_title_selector": "h1",
                "author_selector": ".author",
                "illustration_selector": ".cover img",
                "chapter_link_selector": ".chapters a",
                "toc_next_selector": None,
                "toc_expand_selector": "text=Show all chapters",
            },
            {
                "chapter_title_selector": "h1",
                "chapter_content_selector": ".content",
                "remove_selectors": ["script", "style"],
            },
        )

        self.assertEqual(result["toc_expand_selector"], "text=Show all chapters")
        self.assertEqual(result["illustration_selector"], ".cover img")


if __name__ == "__main__":
    unittest.main()
