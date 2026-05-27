from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.cli import _resolve_config_path, build_parser, build_short_parser


class CliTest(unittest.TestCase):
    def test_short_parser_accepts_novel_and_max_alias(self) -> None:
        args = build_short_parser().parse_args(["sfacg-760079", "--max", "5"])

        self.assertEqual(args.target, "sfacg-760079")
        self.assertEqual(args.max_chapters, 5)

    def test_resolve_config_path_accepts_novel_name(self) -> None:
        self.assertEqual(_resolve_config_path("sfacg-760079"), Path("configs/sfacg-760079.json"))

    def test_resolve_config_path_accepts_direct_path(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.json"
            config_path.write_text("{}", encoding="utf-8")

            self.assertEqual(_resolve_config_path(str(config_path)), config_path)

    def test_validate_parser_exists(self) -> None:
        args = build_parser().parse_args(["validate", "demo"])
        self.assertEqual(args.command, "validate")
        self.assertEqual(args.target, "demo")


if __name__ == "__main__":
    unittest.main()
