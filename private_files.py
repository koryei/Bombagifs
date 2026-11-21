"""Atomic, owner-only file helpers for the self-bot's small data stores (discord-free)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_private_text(path: Path, text: str) -> None:
    """Atomically write text (temp file + rename) readable only by the owner (mode 600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON readable only by the owner (mode 600)."""
    write_private_text(path, json.dumps(data))


def append_private_line(path: Path, line: str) -> None:
    """Append one line to a file that is created with mode 600, flushing it to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
