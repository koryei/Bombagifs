"""``./selfbot-ctl.sh logs`` must read the same before and after the switch to text logs."""

from __future__ import annotations

import json
import unittest

from logfmt import format_line

PREFIX = "Nov 24 23:49:35 bombaclat-vnc python[4112417]: "


def _record(**fields: object) -> str:
    payload: dict[str, object] = {
        "time": "2026-11-25T04:49:35.632840+00:00",
        "level": "INFO",
        "logger": "bombagif.scout",
        "message": "judged a run in chat 1534062466580086784",
    }
    payload.update(fields)
    return json.dumps(payload)


class FormatLineTests(unittest.TestCase):
    def test_a_journald_line_keeps_its_prefix_and_drops_the_duplicate_clock(self) -> None:
        self.assertEqual(
            format_line(f"{PREFIX}{_record()}\n"),
            f"{PREFIX}INFO    scout      judged a run in chat 1534062466580086784\n",
        )

    def test_a_bare_record_keeps_its_clock(self) -> None:
        self.assertRegex(format_line(_record() + "\n"), r"^\d\d:\d\d:\d\d INFO +scout +judged a run")

    def test_context_fields_are_appended(self) -> None:
        line = format_line(_record(trace_id="49761a04", attachment_name="IMG_6635.mov"))
        self.assertTrue(line.endswith("trace_id=49761a04 attachment_name=IMG_6635.mov\n"))

    def test_an_exception_follows_the_line(self) -> None:
        line = format_line(_record(level="ERROR", exception="Traceback (most recent call last):\nValueError: boom"))
        self.assertIn("ValueError: boom", line)
        self.assertTrue(line.endswith("\n"))

    def test_a_text_line_drops_the_clock_journald_already_printed(self) -> None:
        line = f"{PREFIX}23:49:35 INFO    scout      judged a run\n"
        self.assertEqual(format_line(line), f"{PREFIX}INFO    scout      judged a run\n")

    def test_a_bare_text_line_passes_through_untouched(self) -> None:
        line = "23:49:35 INFO    scout      judged a run\n"
        self.assertEqual(format_line(line), line)

    def test_a_broken_record_passes_through_untouched(self) -> None:
        line = f'{PREFIX}{{"time": "2026-11-25T04:49:35+00:00", "message": \n'
        self.assertEqual(format_line(line), line)

    def test_a_foreign_json_line_passes_through_untouched(self) -> None:
        line = f'{PREFIX}{{"time": "2026-11-25T04:49:35+00:00", "note": "not ours"}}\n'
        self.assertEqual(format_line(line), line)


if __name__ == "__main__":
    unittest.main()
