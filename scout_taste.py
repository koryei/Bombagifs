"""What the owner teaches the judge: plain-English taste notes, rated examples, and a tune report (discord-free).

Notes are operator text (trusted, size-capped). Examples are chat lines the owner chose to rate; they are stored with
anonymous speaker labels only and are only ever sent to a provider as data.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from private_files import append_private_line, write_private_text

NOTES_MAX_CHARS = 1500
EXAMPLE_LINE_CHARS = 200
SPEAKER_CHARS = 8
ALL_LIMIT = 500
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # everything but tab and newline

Label = Literal["good", "bad"]


class TasteNotes:
    """``data/scout-taste.txt``: guidance the owner writes (or hand-edits; edits apply on the next judging call)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @property
    def text(self) -> str:
        """The notes, re-read on every access: sanitised and truncated; missing or unreadable means empty."""
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return _CONTROL_RE.sub("", raw).strip()[:NOTES_MAX_CHARS]

    def show(self) -> str:
        return self.text or "(no taste notes yet)"

    @staticmethod
    def clean(line: str) -> str:
        """A line as it would be stored: control characters removed and whitespace (including newlines) collapsed."""
        return " ".join(_CONTROL_RE.sub("", line).split())

    def add(self, line: str) -> bool:
        """Append one line of guidance; False if it is empty or would push the notes past the cap."""
        cleaned = self.clean(line)
        if not cleaned:
            return False
        current = self.text
        combined = f"{current}\n{cleaned}" if current else cleaned
        if len(combined) > NOTES_MAX_CHARS:
            return False
        write_private_text(self.path, combined)
        return True

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            write_private_text(self.path, "")


@dataclass(frozen=True)
class Example:
    """A line (or short exchange) the owner rated, anonymised: ``lines`` are ``(speaker label, text)`` pairs."""

    label: Label
    lines: tuple[tuple[str, str], ...]
    funny: float
    decide: Mapping[str, float]
    source: str
    at: float


def _normalise(lines: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    cleaned = []
    for speaker, text in lines:
        text = " ".join(_CONTROL_RE.sub("", str(text)).split())[:EXAMPLE_LINE_CHARS]
        if text:
            cleaned.append((str(speaker)[:SPEAKER_CHARS], text))
    return tuple(cleaned)


def _number(value: object, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _parse(row: object) -> Example | None:
    if not isinstance(row, dict) or row.get("label") not in ("good", "bad"):
        return None
    raw_lines = row.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        return None
    pairs: list[tuple[str, str]] = []
    for item in raw_lines:
        if not (isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)):
            return None
        pairs.append((item[0], item[1]))
    decide = row.get("decide")
    return Example(
        label=row["label"],
        lines=tuple(pairs),
        funny=_number(row.get("funny")),
        decide={str(k): _number(v) for k, v in decide.items()} if isinstance(decide, dict) else {},
        source=row["source"] if isinstance(row.get("source"), str) else "review",
        at=_number(row.get("at")),
    )


class TasteStore:
    """``data/taste.jsonl``: the owner's rulings (append-only, mode 600)."""

    def __init__(self, path: Path, per_label: int = 8) -> None:
        self.path = Path(path)
        self.per_label = per_label

    def all(self) -> list[Example]:
        """The last 500 valid examples, oldest first (corrupt lines are skipped)."""
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        found: list[Example] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                parsed = _parse(json.loads(line))
            except ValueError:
                continue
            if parsed is not None:
                found.append(parsed)
        return found[-ALL_LIMIT:]

    def examples(self) -> dict[str, list[Example]]:
        """The newest ``per_label`` good and bad examples, newest first."""
        everything = self.all()
        return {
            label: [e for e in reversed(everything) if e.label == label][: self.per_label] for label in ("good", "bad")
        }

    def counts(self) -> dict[str, int]:
        everything = self.all()
        return {label: sum(1 for e in everything if e.label == label) for label in ("good", "bad")}

    def add(
        self,
        label: str,
        lines: Sequence[tuple[str, str]],
        *,
        funny: float = 0.0,
        decide: Mapping[str, float] | None = None,
        source: str = "review",
        at: float | None = None,
    ) -> bool:
        """Record a ruling. False if there is nothing to save or it repeats the latest example with the same label."""
        if label not in ("good", "bad"):
            raise ValueError("label must be 'good' or 'bad'")
        cleaned = _normalise(lines)
        if not cleaned:
            return False
        same = [e for e in self.all() if e.label == label]
        if same and same[-1].lines == cleaned:
            return False
        row = {
            "at": time.time() if at is None else at,
            "label": label,
            "lines": [list(pair) for pair in cleaned],
            "funny": float(funny),
            "decide": {str(k): float(v) for k, v in (decide or {}).items()},
            "source": source,
        }
        append_private_line(self.path, json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return True

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            write_private_text(self.path, "")


def tune_report(examples: Sequence[Example], current_min: float) -> str:
    """Compare the owner's 👍/👎 with the scores and suggest ``SCOUT_JUDGE_FUNNY_MIN`` (needs 5 scored of each)."""
    good = [e.funny for e in examples if e.label == "good" and e.funny > 0]
    bad = [e.funny for e in examples if e.label == "bad" and e.funny > 0]
    if len(good) < 5 or len(bad) < 5:
        return (
            f"Not enough scored examples yet: {len(good)}/5 good and {len(bad)}/5 bad. "
            "Keep reviewing (react 👍/👎 on the review DMs)."
        )
    avg_good, avg_bad = sum(good) / len(good), sum(bad) / len(bad)
    if avg_good <= avg_bad:
        return (
            f"Your 👍 average ({avg_good:.1f}) is not above your 👎 average ({avg_bad:.1f}), so the scores aren't "
            "separating your taste yet. Describe it with `!scout taste add <text>` or rate more examples instead of "
            "changing the threshold."
        )
    suggestion = min(10.0, max(0.0, round((avg_good + avg_bad) / 2, 1)))
    shown = f"{suggestion:g}"
    text = (
        f"Your 👍 averaged {avg_good:.1f} and your 👎 averaged {avg_bad:.1f}. "
        f"Suggested SCOUT_JUDGE_FUNNY_MIN={shown} (now {current_min:g})."
    )
    if avg_bad <= current_min <= avg_good:
        text += " The current value already sits between them."
    return text


class TasteBook:
    """Notes plus examples behind one object, read live on every call (what the judges consume)."""

    def __init__(self, notes: TasteNotes, store: TasteStore) -> None:
        self._notes = notes
        self._store = store

    def notes(self) -> str:
        return self._notes.text

    def examples(self) -> dict[str, list[Example]]:
        return self._store.examples()
