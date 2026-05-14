from __future__ import annotations

import unittest
from unittest.mock import patch

from src.config import Config, SiteConfig


class ConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cfg = Config.from_env()
            self.assertIsNone(cfg.share_dir)
            self.assertEqual(cfg.max_chapters, 0)
            self.assertFalse(cfg.use_browser)

    def test_from_env_reads_env_vars(self) -> None:
        with patch.dict("os.environ", {
            "NOVEL_SHARE_DIR": "/custom/share",
            "MAX_CHAPTERS": "50",
            "USE_BROWSER": "true",
        }, clear=True):
            cfg = Config.from_env()
            self.assertEqual(cfg.share_dir, "/custom/share")
            self.assertEqual(cfg.max_chapters, 50)
            self.assertTrue(cfg.use_browser)

    def test_share_path_expands_user(self) -> None:
        with patch.dict("os.environ", {
            "NOVEL_SHARE_DIR": "~/share",
        }, clear=True):
            cfg = Config.from_env()
            self.assertTrue(str(cfg.share_path).startswith("/"))


class SiteConfigTest(unittest.TestCase):
    def test_from_dict(self) -> None:
        config = SiteConfig.from_dict({
            "name": "test",
            "start_url": "https://example.com",
            "chapter_link_selector": ".chapters a",
            "chapter_content_selector": ".content",
        })
        self.assertEqual(config.name, "test")
        self.assertEqual(config.request_delay_seconds, 1.0)

    def test_from_dict_single_remove_selector(self) -> None:
        config = SiteConfig.from_dict({
            "name": "test",
            "start_url": "https://example.com",
            "chapter_link_selector": ".chapters a",
            "chapter_content_selector": ".content",
            "remove_selectors": "script",
        })
        self.assertEqual(config.remove_selectors, ("script",))


if __name__ == "__main__":
    unittest.main()
