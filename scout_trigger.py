"""Spoken-style triggers for clips (discord-free): "clip that", "gif that", and their typos.

``parse_trigger`` reads one chat message. It returns a ``Trigger`` that is **sure** (the whole message is a known phrase,
filler and laughter aside) or **unsure** (it looks like a request but is typo'd or oddly worded, so an AI check must
confirm it). Everything else, which is almost every message, returns ``None`` and costs nothing.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


MAX_TRIGGER_CHARS = 80
MAX_TRIGGER_TOKENS = 8

_MENTION_RE = re.compile(r"<[@#][!&]?\d+>|@everyone|@here", re.IGNORECASE)
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
_REPEAT_RE = re.compile(r"(.)\1{2,}")
_LAUGH_RES = (
    re.compile(r"^l[lmfaoiu]{2,}$"),  # lol, lmao, lmfao, lmfaii, lool, lolol
    re.compile(r"^(?:ha)+h?$"),
    re.compile(r"^h[ae]h[aeh]*$"),
    re.compile(r"^(?:xd+|kek+|bruh+|ahah[ah]*)$"),
)
_LEET = str.maketrans({"1": "i", "0": "o", "4": "a", "3": "e", "5": "s", "7": "t", "$": "s", "@": "a"})

_FILLER = frozenset(
    "bro bruh bru yo ayo aye fr frfr ong rn now pls plz please dude man guys guy hey ok okay just quick quickly someone somebody "
    "anyone can could would will you u ya we gotta gonna need needs should must to be get got the a an and immediately asap hurry "
    "omg wtf damn dam fam chat gang boys bois buddy mate sir sorry thx ty thanks for me us my real really legit literally lowkey "
    "highkey actually whole entire full um uh uhh hm hmm so like wait bombaclip bombagif bombaclat bot is are was it's thats".split()
)
_OBJECT = frozenset(
    "that this it dat dis tht those these them dem one message msg text moment shi shit line lines quote".split()
)
_CLIP_VERBS = frozenset({"clip", "clips", "clipping"})
_CLIPPED = frozenset({"clipped"})
_GIF_WORDS = frozenset({"gif", "gifs", "jif"})
_GIF_ALLOWED = frozenset({"make", "turn", "convert", "into", "to", "as", "gif", "gifs", "jif"})


@dataclass(frozen=True)
class Trigger:
    """A request found in a message. ``sure`` is False when only the AI check may confirm it."""

    kind: Literal["clip", "gif"]
    sure: bool = True


def edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance: insert, delete, substitute, or swap two neighbours, each costing one."""
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[len(a)][len(b)]


def _tokens(text: str) -> list[str]:
    """Lowercase words with emoji, punctuation and stretched letters ("thattt", "clippp") removed."""
    text = unicodedata.normalize("NFKC", _CUSTOM_EMOJI_RE.sub(" ", text)).lower()
    words: list[str] = []
    for raw in re.split(r"[^\w'$@]+", text):
        word = raw.replace("'", "")
        if not word or not any(ch.isalnum() for ch in word):
            continue
        if any(ch.isalpha() for ch in word):
            word = word.translate(_LEET)
        word = "".join(ch for ch in word if ch.isalnum())
        word = _REPEAT_RE.sub(r"\1", word)
        if word:
            words.append(word)
    return words


def _is_filler(word: str) -> bool:
    return word in _FILLER or any(pattern.match(word) for pattern in _LAUGH_RES)


def _sure_clip(core: list[str], is_reply: bool) -> Trigger | None:
    if not core:
        return None
    verbs = [word for word in core if word in _CLIP_VERBS or word in _CLIPPED]
    if len(verbs) != 1:
        return None
    verb = verbs[0]
    index = core.index(verb)
    before, after = core[:index], core[index + 1 :]
    if before and verb in _CLIPPED and all(word in _OBJECT for word in before) and not after:
        return Trigger("clip")  # "that needs to be clipped"
    if not before and not after:
        return Trigger("clip") if is_reply else None  # a bare "clip" needs a message to point at
    if not before and after and all(word in _OBJECT for word in after):
        return Trigger("clip")
    return None


def _sure_gif(core: list[str], is_reply: bool) -> Trigger | None:
    if not core or not any(word in _GIF_WORDS for word in core):
        return None
    if not all(word in _GIF_ALLOWED or word in _OBJECT for word in core):
        return None
    has_object = any(word in _OBJECT for word in core)
    return Trigger("gif") if has_object or is_reply else None


def _looks_like_clip(tokens: list[str]) -> bool:
    """Close enough to a request to be worth an AI check: the verb (or a typo of it), and a short message."""
    if any(word in _CLIP_VERBS or word in _CLIPPED for word in tokens):
        return True
    has_object = any(word in _OBJECT for word in tokens)
    close = any(len(word) >= 3 and (edit_distance(word, "clip") <= 1 or edit_distance(word, "clipped") <= 1) for word in tokens)
    return close and has_object


def parse_trigger(text: str, *, is_reply: bool = False) -> Trigger | None:
    """Read a chat message as a clip/gif request. ``None`` for everything that is not one."""
    if not text or len(text) > MAX_TRIGGER_CHARS or _MENTION_RE.search(text) or "://" in text or "www." in text.lower():
        return None
    tokens = _tokens(text)
    if not tokens or len(tokens) > MAX_TRIGGER_TOKENS:
        return None
    core = [word for word in tokens if not _is_filler(word)]
    sure = _sure_clip(core, is_reply) or _sure_gif(core, is_reply)
    if sure is not None:
        return sure
    if _looks_like_clip(tokens):
        return Trigger("clip", sure=False)
    return None

