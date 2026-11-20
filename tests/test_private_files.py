"""Atomic, owner-only file helpers shared by the self-bot's data stores."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from private_files import append_private_line, write_private_json


class PrivateFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_write_private_json_roundtrips_atomically_with_mode_600(self) -> None:
        path = self.dir / "nested" / "data.json"
        write_private_json(path, {"a": 1})
        write_private_json(path, {"a": 2, "b": [1, 2]})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 2, "b": [1, 2]})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["data.json"])  # no temp files left behind

    def test_append_private_line_creates_600_file_and_appends_in_order(self) -> None:
        path = self.dir / "log.jsonl"
        append_private_line(path, "first")
        append_private_line(path, "second")
        self.assertEqual(path.read_text(encoding="utf-8").splitlines(), ["first", "second"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_append_creates_parent_dirs(self) -> None:
        path = self.dir / "a" / "b" / "log.jsonl"
        append_private_line(path, "x")
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
