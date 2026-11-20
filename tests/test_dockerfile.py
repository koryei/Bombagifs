"""The app image must ship every first-party module main.py imports."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class DockerfileTests(unittest.TestCase):
    def test_image_includes_core_module(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        copied = " ".join(re.findall(r"^COPY\s+(.+?)\s+\./?\s*$", dockerfile, re.MULTILINE))
        self.assertIn("main.py", copied)
        self.assertIn("bombagif_core.py", copied)
        self.assertRegex(dockerfile, r"chmod 640[^\n]*/app/bombagif_core\.py")

    def test_main_only_imports_known_first_party_modules(self) -> None:
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        first_party = set(re.findall(r"^(?:from|import)\s+(bombagif_\w+|selfbot\w*)\b", main_source, re.MULTILINE))
        self.assertEqual(first_party, {"bombagif_core"})


if __name__ == "__main__":
    unittest.main()
