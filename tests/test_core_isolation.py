"""Guards the rule that the shared core never imports discord."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class CoreIsolationTests(unittest.TestCase):
    def _imports_discord(self, module: str) -> bool:
        code = (
            f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
            f"import {module}; sys.exit(1 if 'discord' in sys.modules else 0)"
        )
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, check=False)
        self.assertIn(result.returncode, (0, 1), result.stderr.decode(errors="replace"))
        return result.returncode == 1

    def test_core_does_not_import_discord(self) -> None:
        self.assertFalse(self._imports_discord("bombagif_core"))

    def test_selfbot_logic_does_not_import_discord(self) -> None:
        self.assertFalse(self._imports_discord("selfbot_logic"))


if __name__ == "__main__":
    unittest.main()
