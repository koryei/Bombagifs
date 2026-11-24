"""Voice clips (discord-free): a rolling per-speaker buffer, the request parser, and the Ogg encoder.

The adapter feeds decoded PCM in as it arrives and asks for a clip when the owner requests one. Everything that
decides *what* a clip contains lives here, so it can be tested without a voice connection: the buffer is a plain
ring of frames, the parser reads "clip what he just said", and the encoder shells out to FFmpeg.

Audio is stored as mono 48 kHz s16le. Discord delivers 48 kHz stereo, so ``add`` downmixes on the way in: half the
memory, and voice clips do not need stereo.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from array import array
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from scout_trigger import VOCABULARY, is_filler, parse_trigger, tokens

logger = logging.getLogger("bombagif.scout")

SAMPLE_RATE = 48000
SAMPLE_WIDTH = 2  # s16le
DEFAULT_KEEP_S = 45.0
DEFAULT_CLIP_S = 30.0
MAX_CLIP_S = 120.0
ENCODE_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class VoiceRequest:
    """What the owner asked for. ``target_id``/``target_name`` pick a person; ``last_speaker`` means "whoever just talked"."""

    seconds: float | None = None
    target_id: int | None = None
    target_name: str | None = None
    last_speaker: bool = False


@dataclass(frozen=True)
class _Frame:
    at: float
    pcm: bytes  # mono s16le


def _downmix(pcm: bytes, channels: int) -> bytes:
    """Stereo s16le to mono s16le by averaging the channels; mono passes through untouched."""
    if channels <= 1:
        return pcm
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % (SAMPLE_WIDTH * channels)])
    mono = array("h", bytes(len(samples) // channels * SAMPLE_WIDTH))
    for index in range(len(mono)):
        base = index * channels
        mono[index] = sum(samples[base : base + channels]) // channels
    return mono.tobytes()


class VoiceBuffer:
    """The last ``keep_s`` seconds of each speaker, so a clip can be cut after the fact."""

    def __init__(self, *, keep_s: float = DEFAULT_KEEP_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.keep_s = keep_s
        self.clock = clock
        self._frames: dict[int, deque[_Frame]] = {}

    def add(self, user_id: int, pcm: bytes, *, channels: int = 2, at: float | None = None) -> None:
        """Append one decoded frame for ``user_id``, dropping anything older than ``keep_s``."""
        if not pcm:
            return
        now = self.clock() if at is None else at
        frames = self._frames.setdefault(user_id, deque())
        frames.append(_Frame(now, _downmix(pcm, channels)))
        cutoff = now - self.keep_s
        while frames and frames[0].at < cutoff:
            frames.popleft()

    def speakers(self) -> list[int]:
        """Everyone with audio still in the buffer, most recent first."""
        return [user_id for user_id, _ in self._recent()]

    def _recent(self) -> list[tuple[int, float]]:
        now = self.clock()
        return sorted(
            ((user_id, frames[-1].at) for user_id, frames in self._frames.items() if frames and frames[-1].at >= now - self.keep_s),
            key=lambda pair: pair[1],
            reverse=True,
        )

    def last_speaker(self, *, within_s: float = 15.0) -> int | None:
        """Whoever spoke most recently, or ``None`` if nobody has in ``within_s``."""
        recent = self._recent()
        if not recent:
            return None
        user_id, at = recent[0]
        return user_id if self.clock() - at <= within_s else None

    def clip(self, *, seconds: float = DEFAULT_CLIP_S, user_id: int | None = None) -> bytes:
        """The last ``seconds`` as mono s16le, ending now. Short buffers are padded with silence at the front.

        ``user_id`` picks one speaker; ``None`` mixes everyone who talked in the window.
        """
        seconds = max(0.0, min(seconds, MAX_CLIP_S))
        length = int(seconds * SAMPLE_RATE) * SAMPLE_WIDTH
        if not length:
            return b""
        now = self.clock()
        start = now - seconds
        chosen = [user_id] if user_id is not None else list(self._frames)
        frames = [
            frame
            for speaker in chosen
            for frame in self._frames.get(speaker, ())
            if frame.at >= start
        ]
        if not frames:
            return b""
        if len(chosen) == 1:
            return self._single(frames, start, length)
        return self._mixed(frames, start, length)

    @staticmethod
    def _single(frames: Sequence[_Frame], start: float, length: int) -> bytes:
        """One speaker: concatenate, then align the end of the audio with the end of the clip."""
        joined = b"".join(frame.pcm for frame in frames)
        if len(joined) >= length:
            return joined[-length:]
        return bytes(length - len(joined)) + joined

    @staticmethod
    def _mixed(frames: Sequence[_Frame], start: float, length: int) -> bytes:
        """Several speakers: place each frame by its timestamp and sum, clipping at the s16 range."""
        out = array("h", bytes(length))
        for frame in frames:
            offset = int((frame.at - start) * SAMPLE_RATE) * SAMPLE_WIDTH
            samples = array("h")
            samples.frombytes(frame.pcm)
            if offset < 0:
                samples = samples[-offset // SAMPLE_WIDTH :]
                offset = 0
            index = offset // SAMPLE_WIDTH
            for value in samples:
                if index >= len(out):
                    break
                mixed = out[index] + value
                out[index] = 32767 if mixed > 32767 else (-32768 if mixed < -32768 else mixed)
                index += 1
        return out.tobytes()

    def clear(self) -> None:
        self._frames.clear()


_PRONOUNS = frozenset({"he", "she", "they", "him", "her", "them"})
# Read from the raw text: the tokenizer applies leet substitutions, which turn "30s" into "eos".
_DURATION_RE = re.compile(r"\b(?:last\s+)?(\d{1,3})\s*(?:s|sec|secs|seconds?)\b", re.IGNORECASE)


def _take_seconds(text: str) -> tuple[float | None, str]:
    """Pull "30 seconds" / "30s" out of the message, returning the duration and the text without it."""
    match = _DURATION_RE.search(text)
    if match is None:
        return None, text
    value = int(match.group(1))
    seconds = float(value) if 0 < value <= MAX_CLIP_S else None
    return seconds, text[: match.start()] + " " + text[match.end() :]


def _take_pronoun(words: list[str]) -> bool:
    """Pull "what he just said" out of the tokens: the owner means whoever spoke last."""
    for index, word in enumerate(words):
        if word != "what":
            continue
        rest = words[index + 1 : index + 4]
        if rest and rest[0] in _PRONOUNS:
            end = index + 2
            if len(words) > end and words[end] == "just":
                end += 1
            if len(words) > end and words[end] == "said":
                del words[index : end + 1]
                return True
    return False


def match_members(word: str, members: Sequence[tuple[int, str]]) -> list[int]:
    """Members a word names: an exact name wins over a prefix, so "pranit" is not ambiguous with "Pranit Two"."""
    exact = [user_id for user_id, name in members if name.lower() == word]
    if exact:
        return exact
    return [user_id for user_id, name in members if name.lower().startswith(word)]


def _take_member(words: list[str], members: Sequence[tuple[int, str]]) -> tuple[int | None, str | None]:
    """Pull a member's name out of the tokens. Returns the id when exactly one member matches, else the raw name.

    A single leftover word that names nobody is treated as a name the bot does not know, so the owner is told
    rather than getting a clip of the wrong person. Two or more leftovers are just noise.
    """
    leftovers: list[int] = []
    for index, word in enumerate(words):
        if len(word) < 2 or word in VOCABULARY or is_filler(word):
            continue
        matches = match_members(word, members)
        if len(matches) == 1:
            del words[index]
            return matches[0], None
        if len(matches) > 1:
            del words[index]
            return None, word
        leftovers.append(index)
    if len(leftovers) == 1:
        name = words.pop(leftovers[0])
        return None, name
    return None, None


def parse_voice_request(text: str, members: Sequence[tuple[int, str]] = ()) -> VoiceRequest | None:
    """Read a chat message as a voice-clip request: "clip that", "clip Pranit that", "clip what he just said".

    ``members`` is ``(user_id, display name)`` for everyone in the call, so a name can be resolved. ``None`` for
    everything that is not a request, including "gif that" (there is no voice GIF).
    """
    seconds, text = _take_seconds(text)
    words = tokens(text)
    if not words:
        return None
    last_speaker = _take_pronoun(words)
    target_id, target_name = _take_member(words, members)
    # A named or implied target is the "message" a bare "clip" points at, so it counts as a reply.
    trigger = parse_trigger(
        " ".join(words), is_reply=bool(last_speaker or target_id or target_name or seconds is not None)
    )
    # Voice has no AI check behind it, so only a phrase the parser is sure about counts.
    if trigger is None or trigger.kind != "clip" or not trigger.sure:
        return None
    return VoiceRequest(seconds=seconds, target_id=target_id, target_name=target_name, last_speaker=last_speaker)


def resolve_target(
    request: VoiceRequest, buffer: VoiceBuffer, members: Sequence[tuple[int, str]]
) -> tuple[int | None, str | None]:
    """Pick the speaker to clip. Returns ``(user_id, problem)``; ``(None, None)`` means "mix everyone"."""
    if request.target_id is not None:
        if any(user_id == request.target_id for user_id, _ in members):
            return request.target_id, None
        return None, "I don't see that person in the call."
    if request.target_name is not None:
        matches = match_members(request.target_name, members)
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            return None, f"I don't know who {request.target_name} is."
        return None, f"Which {request.target_name}? Say the whole name."
    return buffer.last_speaker(), None


def encode_ogg(pcm: bytes, *, sample_rate: int = SAMPLE_RATE, timeout: float = ENCODE_TIMEOUT_S) -> bytes:
    """Encode mono s16le PCM to Ogg/Opus, which Discord plays inline. Raises ``RuntimeError`` on failure."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("Voice clips need FFmpeg. Ask the bot host to install it and restart Bombagif.")
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-c:a",
        "libopus",
        "-b:a",
        "48k",
        "-application",
        "voip",
        "-f",
        "ogg",
        "pipe:1",
    ]
    try:
        result = subprocess.run(command, input=pcm, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Encoding the voice clip timed out.") from exc
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip()[:300]
        logger.warning("FFmpeg could not encode a voice clip (returncode=%s): %s", result.returncode, detail)
        raise RuntimeError("That voice clip could not be encoded.")
    return result.stdout


def format_voice_notice(name: str, seconds: float) -> str:
    """The caption posted with a voice clip."""
    return f"🎙️ **{name}**, last {int(seconds)}s"
