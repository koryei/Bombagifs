"""The scout's judges: anonymised windows, verdict validation, provider clients, and the fallback chain.

Discord-free. Text from chats is untrusted data: it is only ever sent to a provider as a field in a JSON
payload with anonymous speaker labels, and whatever comes back is validated before anything acts on it.
"""

from __future__ import annotations

import math
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from scout_buffer import ScoutMessage

SENSITIVE_FLAGS = (
    "personal_info",
    "credentials",
    "self_harm",
    "sexual",
    "slur",
    "minor",
    "health",
    "tragedy",
    "other",
)
NOT_TEXT = "[not text]"
MAX_CLIP_MESSAGES = 8
MAX_CLIP_CHARS = 600
MAX_CLIP_AUTHORS = 3
MAX_REASON_CHARS = 160


@dataclass(frozen=True)
class JudgeLine:
    """One line of what a provider sees: an anonymous speaker label, the text, and whether it was deleted."""

    speaker: str
    text: str
    deleted: bool


@dataclass(frozen=True)
class JudgeWindow:
    """The lines sent to a judge plus the buffered messages they came from (never sent anywhere)."""

    lines: tuple[JudgeLine, ...]
    messages: tuple[ScoutMessage, ...] = field(repr=False)
    run_start: int
    run_end: int


def _speaker_label(index: int) -> str:
    letters = string.ascii_uppercase
    return letters[index] if index < len(letters) else f"S{index + 1}"


def build_judge_window(messages: Sequence[ScoutMessage], run: Sequence[ScoutMessage]) -> JudgeWindow:
    """Anonymise a buffer window: speakers become A, B, C by first appearance, text is the original version.

    Ineligible (non-text) messages become ``[not text]`` placeholder lines without a speaker label.
    """
    labels: dict[int, str] = {}
    lines: list[JudgeLine] = []
    for message in messages:
        if not message.eligible:
            lines.append(JudgeLine("-", NOT_TEXT, message.deleted))
            continue
        label = labels.setdefault(message.author_id, _speaker_label(len(labels)))
        lines.append(JudgeLine(label, message.original, message.deleted))
    positions = {message.id: index for index, message in enumerate(messages)}
    return JudgeWindow(
        lines=tuple(lines),
        messages=tuple(messages),
        run_start=positions[run[0].id],
        run_end=positions[run[-1].id],
    )


def window_payload(window: JudgeWindow) -> list[dict[str, object]]:
    """The only representation of a window that may leave the machine: no names, ids, or avatars."""
    return [
        {"i": index, "speaker": line.speaker, "text": line.text, "deleted": line.deleted}
        for index, line in enumerate(window.lines)
    ]


@dataclass(frozen=True)
class Verdict:
    """A judge's decision about a window. ``start``/``end`` are inclusive line indexes into the window."""

    clip: bool
    start: int
    end: int
    funny: float
    standalone: bool
    sensitive: tuple[str, ...]
    reason: str
    decide: Mapping[str, float]
    provider: str


class ProviderUnavailable(Exception):
    """The provider is gone or refuses us (auth, billing, not found, shut down): stop using it."""


class ProviderError(Exception):
    """A transient provider failure (timeouts, 5xx, malformed responses): try again later."""


class Judge(Protocol):
    """Anything that can judge a window. ``None`` means "no verdict" (the provider failed or is out of budget)."""

    name: str
    can_post: bool

    async def judge(self, window: JudgeWindow) -> Verdict | None: ...


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_verdict(
    raw: object,
    window: JudgeWindow,
    *,
    provider: str,
    decide: Mapping[str, float] | None = None,
) -> Verdict | None:
    """Turn a provider's raw object into a ``Verdict``, or ``None`` if it is malformed or out of range.

    Types are strict (``True`` is not an int, ``1`` is not a bool). When ``clip`` is true the chosen range must be
    contiguous inside the window, at most 8 lines / 600 characters / 3 speakers, with no ``[not text]`` line.
    """
    if not isinstance(raw, dict):
        return None
    clip, standalone, start, end = raw.get("clip"), raw.get("standalone"), raw.get("start"), raw.get("end")
    funny, sensitive, reason = raw.get("funny"), raw.get("sensitive"), raw.get("reason")
    if not isinstance(clip, bool) or not isinstance(standalone, bool):
        return None
    if not _is_int(start) or not _is_int(end):
        return None
    if isinstance(funny, bool) or not isinstance(funny, (int, float)) or not math.isfinite(funny) or not 0 <= funny <= 10:
        return None
    if not isinstance(sensitive, list) or not all(isinstance(flag, str) and flag in SENSITIVE_FLAGS for flag in sensitive):
        return None
    if not isinstance(reason, str):
        return None
    if clip:
        lines = window.lines
        if not 0 <= start <= end < len(lines):
            return None
        chosen = lines[start : end + 1]
        if len(chosen) > MAX_CLIP_MESSAGES or sum(len(line.text) for line in chosen) > MAX_CLIP_CHARS:
            return None
        if any(line.speaker == "-" for line in chosen) or len({line.speaker for line in chosen}) > MAX_CLIP_AUTHORS:
            return None
    return Verdict(
        clip=clip,
        start=start,
        end=end,
        funny=float(funny),
        standalone=standalone,
        sensitive=tuple(sensitive),
        reason=reason[:MAX_REASON_CHARS],
        decide=dict(decide or {}),
        provider=provider,
    )


class HeuristicJudge:
    """The last-resort judge: without an LLM there is no humour or sensitivity judgement, so it never clips."""

    name = "heuristic"
    can_post = False

    async def judge(self, window: JudgeWindow) -> Verdict | None:
        return Verdict(
            clip=False,
            start=0,
            end=0,
            funny=0.0,
            standalone=False,
            sensitive=(),
            reason="no LLM judge",
            decide={},
            provider=self.name,
        )
