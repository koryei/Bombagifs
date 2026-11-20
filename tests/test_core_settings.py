"""Settings loading shared by the slash-command app and the self-bot."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from bombagif_core import read_settings

ZIPLINE = {"ZIPLINE_TOKEN": "z", "ZIPLINE_URL": "https://z.example"}


class ReadSettingsTests(unittest.TestCase):
    def test_read_settings_uses_named_token_env(self) -> None:
        with patch.dict(os.environ, {"SELFBOT_TOKEN": "t", **ZIPLINE}, clear=True):
            self.assertEqual(read_settings("SELFBOT_TOKEN").discord_token, "t")

    def test_default_token_env_is_discord_token(self) -> None:
        with patch.dict(os.environ, {"DISCORD_TOKEN": "d", **ZIPLINE}, clear=True):
            self.assertEqual(read_settings().discord_token, "d")

    def test_missing_named_token_names_the_variable(self) -> None:
        with patch.dict(os.environ, {"DISCORD_TOKEN": "d", **ZIPLINE}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SELFBOT_TOKEN"):
                read_settings("SELFBOT_TOKEN")


if __name__ == "__main__":
    unittest.main()
