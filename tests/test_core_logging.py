"""Log lines a human reads: one plain line per record, JSON only when asked for."""

from __future__ import annotations

import logging
import sys
import unittest

from bombagif_core import JsonFormatter, TextFormatter, configure_logging


def _record(level: int = logging.INFO, message: str = "judged a run in chat %s", args: tuple = (123,)) -> logging.LogRecord:
    return logging.LogRecord("bombagif.scout", level, __file__, 1, message, args, None)


class TextFormatterTests(unittest.TestCase):
    def test_one_plain_line_with_the_short_logger_name(self) -> None:
        line = TextFormatter().format(_record())
        self.assertRegex(line, r"^\d\d:\d\d:\d\d INFO +scout +judged a run in chat 123$")
        self.assertNotIn("{", line)

    def test_levels_line_up(self) -> None:
        info = TextFormatter().format(_record())
        warning = TextFormatter().format(_record(logging.WARNING, "FFmpeg failed", ()))
        self.assertEqual(info.index("scout"), warning.index("scout"))

    def test_context_fields_are_appended(self) -> None:
        record = _record()
        record.trace_id = "49761a04"
        record.attachment_name = "IMG_6635.mov"
        self.assertTrue(TextFormatter().format(record).endswith("trace_id=49761a04 attachment_name=IMG_6635.mov"))

    def test_an_exception_follows_the_line(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord("bombagif", logging.ERROR, __file__, 1, "scout tick failed", (), sys.exc_info())
        first, *rest = TextFormatter().format(record).splitlines()
        self.assertRegex(first, r"^\d\d:\d\d:\d\d ERROR +bombagif +scout tick failed$")
        self.assertIn("ValueError: boom", "\n".join(rest))


class ConfigureLoggingTests(unittest.TestCase):
    def tearDown(self) -> None:
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)
        logging.getLogger("discord").setLevel(logging.NOTSET)

    def test_text_is_the_default(self) -> None:
        configure_logging("INFO")
        self.assertIsInstance(logging.getLogger().handlers[0].formatter, TextFormatter)

    def test_json_is_opt_in(self) -> None:
        configure_logging("INFO", "json")
        self.assertIsInstance(logging.getLogger().handlers[0].formatter, JsonFormatter)

    def test_discord_chatter_is_hidden_until_debug(self) -> None:
        configure_logging("INFO")
        self.assertEqual(logging.getLogger("discord").level, logging.WARNING)
        configure_logging("DEBUG")
        self.assertEqual(logging.getLogger("discord").level, logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
