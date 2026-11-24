"""Configuration for the clip scout, read from the environment and validated (discord-free)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

Mode = Literal["off", "dry-run", "on"]
JudgeChoice = Literal["auto", "inception", "openai", "heuristic"]
Screen = Literal["all", "signals"]
DeletedPolicy = Literal["on", "owner", "off"]
Theme = Literal["onyx", "dark"]
ReviewMode = Literal["off", "dry-run", "always"]
TriggerMode = Literal["off", "phrases", "ai"]

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class ScoutConfig:
    """Every scout setting, with the defaults from the design spec."""

    mode: Mode = "off"
    owner_id: int | None = None
    seed_chats: frozenset[int] = frozenset()
    judge: JudgeChoice = "auto"
    inception_api_key: str = field(default="", repr=False)
    llm_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    screen: Screen = "all"
    decide_funny_min: float = 0.85
    decide_standalone_min: float = 0.80
    decide_sensitive_max: float = 0.10
    judge_funny_min: float = 8.0
    deleted_funny_min: float = 9.0
    deleted_sensitive_max: float = 0.05
    clip_deleted: DeletedPolicy = "on"
    chat_cooldown_s: int = 15 * 60
    author_cooldown_s: int = 120 * 60
    max_per_hour: int = 4
    buffer_minutes: int = 15
    buffer_max_per_chat: int = 200
    reaction_window_s: int = 45
    tick_s: int = 5
    max_calls_per_day: int = 2000
    theme: Theme = "onyx"
    manual: bool = True
    review: ReviewMode = "dry-run"
    review_per_hour: int = 6
    review_batch: int = 10
    review_batch_minutes: int = 15
    near_miss_decide_funny: float = 0.6
    near_miss_final_funny: float = 6.0
    examples_per_label: int = 8
    manual_limit_per_10_min: int = 3
    manual_author_cooldown_s: int = 120
    triggers: TriggerMode = "ai"
    trigger_anyone: bool = False
    trigger_ai_per_day: int = 300
    trigger_max_age_s: int = 180


def _text(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _choice(env: Mapping[str, str], name: str, allowed: tuple[str, ...], default: str) -> str:
    value = _text(env, name).lower() or default
    if value not in allowed:
        raise RuntimeError(f"{name} must be one of: {', '.join(allowed)}")
    return value


def _number(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number between {low:g} and {high:g}") from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise RuntimeError(f"{name} must be a number between {low:g} and {high:g}")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive whole number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive whole number")
    return value


def _ids(raw: str, name: str) -> list[int]:
    found: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError as exc:
            raise RuntimeError(f"{name} must contain positive numeric IDs") from exc
        if value <= 0:
            raise RuntimeError(f"{name} must contain positive numeric IDs")
        found.append(value)
    return found


def _base_url(raw: str) -> str:
    """Validate SCOUT_LLM_BASE_URL: https (http only for loopback), no credentials, query, or fragment."""
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        host = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError("SCOUT_LLM_BASE_URL must be a valid URL") from exc
    secure = parsed.scheme == "https" or (parsed.scheme == "http" and host in _LOOPBACK_HOSTS)
    if not host or not secure or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise RuntimeError(
            "SCOUT_LLM_BASE_URL must be an https base URL (http only for localhost) without credentials or query parameters"
        )
    return raw.rstrip("/")


def read_scout_config(env: Mapping[str, str]) -> ScoutConfig:
    """Read and validate every scout setting; errors name the offending variable."""
    owner_raw = _text(env, "SELFBOT_OWNER_ID")
    owner = _ids(owner_raw, "SELFBOT_OWNER_ID") if owner_raw else []
    if len(owner) > 1:
        raise RuntimeError("SELFBOT_OWNER_ID must be a single numeric ID")
    cfg = ScoutConfig(
        mode=_choice(env, "SELFBOT_SCOUT_MODE", ("off", "dry-run", "on"), "off"),  # type: ignore[arg-type]
        owner_id=owner[0] if owner else None,
        seed_chats=frozenset(_ids(_text(env, "SELFBOT_SCOUT_CHATS"), "SELFBOT_SCOUT_CHATS")),
        judge=_choice(env, "SCOUT_JUDGE", ("auto", "inception", "openai", "heuristic"), "auto"),  # type: ignore[arg-type]
        inception_api_key=_text(env, "INCEPTION_API_KEY"),
        llm_base_url=_base_url(_text(env, "SCOUT_LLM_BASE_URL")),
        llm_api_key=_text(env, "SCOUT_LLM_API_KEY"),
        llm_model=_text(env, "SCOUT_LLM_MODEL"),
        screen=_choice(env, "SCOUT_SCREEN", ("all", "signals"), "all"),  # type: ignore[arg-type]
        decide_funny_min=_number(env, "SCOUT_DECIDE_FUNNY_MIN", 0.85, 0.0, 1.0),
        decide_standalone_min=_number(env, "SCOUT_DECIDE_STANDALONE_MIN", 0.80, 0.0, 1.0),
        decide_sensitive_max=_number(env, "SCOUT_DECIDE_SENSITIVE_MAX", 0.10, 0.0, 1.0),
        judge_funny_min=_number(env, "SCOUT_JUDGE_FUNNY_MIN", 8.0, 0.0, 10.0),
        deleted_funny_min=_number(env, "SCOUT_DELETED_FUNNY_MIN", 9.0, 0.0, 10.0),
        deleted_sensitive_max=_number(env, "SCOUT_DELETED_SENSITIVE_MAX", 0.05, 0.0, 1.0),
        clip_deleted=_choice(env, "SELFBOT_CLIP_DELETED", ("on", "owner", "off"), "on"),  # type: ignore[arg-type]
        chat_cooldown_s=_positive_int(env, "SCOUT_CHAT_COOLDOWN_MINUTES", 15) * 60,
        author_cooldown_s=_positive_int(env, "SCOUT_AUTHOR_COOLDOWN_MINUTES", 120) * 60,
        max_per_hour=_positive_int(env, "SCOUT_MAX_PER_HOUR", 4),
        buffer_minutes=_positive_int(env, "SCOUT_BUFFER_MINUTES", 15),
        buffer_max_per_chat=_positive_int(env, "SCOUT_BUFFER_MAX_PER_CHAT", 200),
        reaction_window_s=_positive_int(env, "SCOUT_REACTION_WINDOW_SECONDS", 45),
        tick_s=_positive_int(env, "SCOUT_TICK_SECONDS", 5),
        max_calls_per_day=_positive_int(env, "SCOUT_MAX_CALLS_PER_DAY", 2000),
        theme=_choice(env, "CLIP_THEME", ("onyx", "dark"), "onyx"),  # type: ignore[arg-type]
        manual=_choice(env, "SCOUT_MANUAL", ("on", "off"), "on") == "on",
        review=_choice(env, "SCOUT_REVIEW", ("off", "dry-run", "always"), "dry-run"),  # type: ignore[arg-type]
        review_per_hour=_positive_int(env, "SCOUT_REVIEW_PER_HOUR", 6),
        review_batch=_positive_int(env, "SCOUT_REVIEW_BATCH", 10),
        review_batch_minutes=_positive_int(env, "SCOUT_REVIEW_BATCH_MINUTES", 15),
        near_miss_decide_funny=_number(env, "SCOUT_NEAR_MISS_DECIDE_FUNNY", 0.6, 0.0, 1.0),
        near_miss_final_funny=_number(env, "SCOUT_NEAR_MISS_FINAL_FUNNY", 6.0, 0.0, 10.0),
        examples_per_label=_positive_int(env, "SCOUT_EXAMPLES", 8),
        manual_limit_per_10_min=_positive_int(env, "SCOUT_MANUAL_LIMIT_PER_10_MIN", 3),
        manual_author_cooldown_s=_positive_int(env, "SCOUT_MANUAL_AUTHOR_COOLDOWN_SECONDS", 120),
        triggers=_choice(env, "SCOUT_TRIGGERS", ("off", "phrases", "ai"), "ai"),  # type: ignore[arg-type]
        trigger_anyone=_choice(env, "SCOUT_TRIGGER_ANYONE", ("on", "off"), "off") == "on",
        trigger_ai_per_day=_positive_int(env, "SCOUT_TRIGGER_AI_PER_DAY", 300),
        trigger_max_age_s=_positive_int(env, "SCOUT_TRIGGER_MAX_AGE_SECONDS", 180),
    )
    if cfg.judge == "openai":
        for name, value in (
            ("SCOUT_LLM_BASE_URL", cfg.llm_base_url),
            ("SCOUT_LLM_API_KEY", cfg.llm_api_key),
            ("SCOUT_LLM_MODEL", cfg.llm_model),
        ):
            if not value:
                raise RuntimeError(f"{name} must be set when SCOUT_JUDGE=openai")
    return cfg
