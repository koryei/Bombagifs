"""Eligibility rules and the local sensitive-content filter for clips (discord-free).

The filter is deliberately conservative and always wins: whatever the judge says, a window that trips any
rule here is never clipped. False positives only cost a missed joke.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MIN_CHARS = 4
MAX_CHARS = 400

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_SPOILER_RE = re.compile(r"\|\|.+?\|\|", re.DOTALL)
_COMMAND_RE = re.compile(r"^[!/.][A-Za-z]")


def is_eligible_text(text: str) -> bool:
    """True if a message's text may ever be buffered as clippable (length, no links/spoilers/commands)."""
    stripped = text.strip()
    if not MIN_CHARS <= len(stripped) <= MAX_CHARS:
        return False
    if _URL_RE.search(stripped) or _SPOILER_RE.search(stripped) or _COMMAND_RE.match(stripped):
        return False
    return True


@dataclass(frozen=True)
class SafetyResult:
    """Outcome of the local filter: ``ok`` only when nothing sensitive was found."""

    ok: bool
    reasons: tuple[str, ...] = ()


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_DIGIT_RUN_RE = re.compile(r"\+?\(?\d[\d\s().\-]{7,}\d")
_IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
_IPV6_RES = (
    re.compile(r"(?<![0-9A-Fa-f:])[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){4,7}(?![0-9A-Fa-f:])"),
    re.compile(r"(?<![0-9A-Fa-f:])[0-9A-Fa-f]{0,4}::[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*(?![0-9A-Fa-f:])"),
)
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{6,}|AIza[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{12,})"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{6,}",
)
_INVITE_RE = re.compile(r"discord\.gg/\w+|discord(?:app)?\.com/invite/\w+", re.IGNORECASE)
_ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+(?:[A-Za-z][A-Za-z.'-]*\s+){1,3}"
    r"(?:street|st|avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd|court|ct)\b",
    re.IGNORECASE,
)
_MASS_MENTION_RE = re.compile(r"(?<!\w)@(?:everyone|here)\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _find_reasons(text: str, blocklist: frozenset[str]) -> set[str]:
    reasons: set[str] = set()
    if _EMAIL_RE.search(text):
        reasons.add("email")
    for match in _DIGIT_RUN_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 9 <= len(digits) <= 15:
            reasons.add("phone")  # E.164 numbers have at most 15 digits
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            reasons.add("card")
    for match in _IPV4_RE.finditer(text):
        if all(int(octet) <= 255 for octet in match.groups()):
            reasons.add("ip")
    if any(pattern.search(text) for pattern in _IPV6_RES):
        reasons.add("ip")
    for match in _LONG_TOKEN_RE.finditer(text):
        token = match.group()
        if any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
            reasons.add("token")
    if _KEY_RE.search(text):
        reasons.add("token")
    if _INVITE_RE.search(text):
        reasons.add("invite")
    if _ADDRESS_RE.search(text):
        reasons.add("address")
    if _MASS_MENTION_RE.search(text):
        reasons.add("mention")
    lowered = text.lower()
    for term in blocklist:
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", lowered):
            reasons.add("blocklist")
            break
    return reasons


def check_text(text: str, blocklist: frozenset[str] = frozenset()) -> SafetyResult:
    """Scan one text for personal info, credentials, invites, mass mentions, and blocklisted terms."""
    reasons = _find_reasons(text, blocklist)
    return SafetyResult(not reasons, tuple(sorted(reasons)))


def check_window(texts: Sequence[str], blocklist: frozenset[str] = frozenset()) -> SafetyResult:
    """Scan each text and the whole window joined, so a secret split across messages is still caught."""
    if not texts:
        return SafetyResult(True)
    reasons = _find_reasons("\n".join(texts), blocklist)
    for text in texts:
        reasons |= _find_reasons(text, blocklist)
    return SafetyResult(not reasons, tuple(sorted(reasons)))


def load_blocklist(path: Path) -> frozenset[str]:
    """Read one lowercase term per line (``#`` comments and blank lines ignored); a missing file is empty."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    terms = set()
    for line in raw.splitlines():
        term = line.split("#", 1)[0].strip().lower()
        if term:
            terms.add(term)
    return frozenset(terms)
