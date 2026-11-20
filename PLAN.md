# Bombagif v2 (self-bot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** implemented on branch `v2-selfbot` (all 16 tasks). Deviations: `SelfBot` keeps `Settings` as `self.config` (the library owns `Client.settings`); Tasks 12 and 14 were split into several commits; `convert_and_upload` takes an optional `log_context`; embeds are fetched only through Discord proxy URLs (bare links in a ping's own text are not); the Dockerfile now copies `bombagif_core.py` (found in final review); plain DM replies with no media get no "no media" answer.

**Goal:** Add a Discord user-account "self-bot" front door: people who DMed it first can ping it with media, or ping it while replying to media, and get the Zipline GIF link back in the same chat.

**Architecture:** Extract the discord-free conversion/upload pipeline into `bombagif_core.py` (shared by the existing `/gif` app and the new self-bot). Decision logic lives in a discord-free `selfbot_logic.py` (unit-tested with fakes); `selfbot.py` is a thin `discord.py-self` adapter run from its own venv.

**Tech Stack:** Python 3.11+, `discord.py-self` 2.1.0 (self-bot only), `discord.py` 2.7.1 (app only), aiohttp, Pillow, FFmpeg, `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-18-selfbot-design.md` (v1 = the existing `/gif` app, **v2 = this plan**, v3 = the autonomous clip scout, out of scope here).

## Global Constraints

- `discord.py-self` and `discord.py` both install as `import discord`: **never import `discord` in `bombagif_core.py` or `selfbot_logic.py`**; run the self-bot from its own venv (`.venv-selfbot`, gitignored).
- Only users who have **DMed the account first** are served. Everyone else is ignored silently (no replies, no errors).
- Rate limit: 5 triggers per user per 60 s; excess is ignored silently. Batch cap: `MAX_GIF_BATCH` (5) files per trigger.
- Replies: one message per trigger, `reply(..., mention_author=False)`, `AllowedMentions.none()`, bare links so Discord embeds the GIF.
- Env: `SELFBOT_TOKEN` (required for the self-bot), `SELFBOT_DATA_DIR` (default `./data`), plus existing `ZIPLINE_URL`, `ZIPLINE_TOKEN`, `GIF_BACKGROUND`, `LOG_LEVEL`. The app keeps requiring `DISCORD_TOKEN`; neither program requires the other's token.
- Never log or echo the token. The allowlist file is mode `600`.
- Every commit leaves `python3 -m unittest discover -s tests` green (54 tests at the start) and ends with the trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. One task = one commit unless a step says otherwise.
- Run commands from the repo root. Tests for `main` run in system `python3` (has `discord.py`); self-bot adapter tests run in `.venv-selfbot` and skip elsewhere.

## Review Focus

1. A ping that replies to a **deleted or inaccessible** message: use the pinging message's own media; if none, reply "no supported image or video found"; never crash. (Task 8, 12)
2. **Corrupt or unreadable allowlist file**: start empty, move the bad file aside as `allowlist.json.corrupt`, log a warning, never crash or silently overwrite it. (Task 5)
3. **Role / `@everyone` mentions and non-user pings** must not trigger; only a direct mention of the self-bot (including a reply-ping to it) does. A non-allowlisted `stop` is ignored (no welcome). (Task 7)
4. **More than 5 media items, or unsupported files mixed with supported ones**: cap at 5 with a "(+N skipped)" note; unsupported files are silently skipped, not errors. (Task 8, 9)
5. **Embed media from a non-Discord host** (SSRF): only Discord-proxied media URLs are fetched; anything else is dropped. (Task 14)

---

## Phase A — Extract the shared core (no behaviour change)

### Task 1: Move conversion code into `bombagif_core.py`

**Files:**
- Create: `bombagif_core.py`
- Modify: `main.py` (delete moved code, import it), `tests/test_bombagif.py` (repoint patches)

**Interfaces:**
- Produces: everything from `main.py` lines 1–72 constants, `UserFacingError`, `InvalidImage`, `_CandidateTooLarge`, `UploadFailure`, `_check_svg`, `_svg_length`, `_BoundedBytesIO`, `_rasterize_svg`, `convert_video`, `_convert_video_once`, `_is_avif_container`, `_decode_avif_with_ffmpeg`, `_convert_image_to_target`, `convert_image`, `_convert_image_once`, `validate_attachment_metadata`, `validate_image_metadata` — same names, same signatures, importable from `bombagif_core`.

- [ ] **Step 1:** Move the listed names verbatim into `bombagif_core.py` (module docstring, `from __future__ import annotations`, only the imports they need; **no `discord` import**). In `main.py` replace them with `from bombagif_core import (...)` listing every name `main.py` or `tests/` still uses via `main.<name>`.
- [ ] **Step 2:** In `tests/test_bombagif.py` repoint `patch("main._convert_image_once", ...)`, `patch("main._convert_video_once", ...)` to `bombagif_core.*`; `patch("main.shutil.which")` / `patch("main.importlib.import_module")` keep working only if `main.py` still imports `shutil`/`importlib` — switch them to `bombagif_core.shutil.which` / `bombagif_core.importlib.import_module` instead of keeping unused imports.
- [ ] **Step 3:** Add `tests/test_core_isolation.py::test_core_does_not_import_discord`: run `python3 -I -c "import bombagif_core, sys; sys.exit('discord' in sys.modules)"` with `cwd` = repo root via `subprocess.run([...], env={**os.environ, "PYTHONPATH": repo_root})`; assert return code `0`. (`-I` ignores PYTHONPATH, so use `sys.path.insert(0, root)` in the `-c` string instead of `PYTHONPATH`.)
- [ ] **Step 4:** Run `python3 -m unittest discover -s tests` → all pass (54 + 1).
- [ ] **Step 5:** Commit `Extract image and video conversion into bombagif_core`

### Task 2: Move download/upload into core

**Files:** Modify `bombagif_core.py`, `main.py`, `tests/test_bombagif.py` (only if patch targets move).

**Interfaces:**
- Produces in core: `read_bounded`, `download_attachment(session, attachment)` (duck-typed: needs `.filename .content_type .size .url`, annotation uses a `Protocol` named `MediaLike`, not `discord.Attachment`), `_retry_delay`, `_extract_zipline_file_url`, `upload_to_zipline(session, settings, content, filename, *, trace_id)`. `Settings` and `_read_settings` stay in `main.py` for now (Task 3), so `upload_to_zipline` types `settings` as `Settings` via `TYPE_CHECKING`-only import or the core `Settings` once Task 3 lands — move `Settings` here in the same step to avoid a cycle.

- [ ] **Step 1:** Move the functions and the `Settings` dataclass; `main.py` imports them. Keep `_read_settings` in `main.py` unchanged for this commit.
- [ ] **Step 2:** Run the full suite → green.
- [ ] **Step 3:** Commit `Move download and Zipline upload into bombagif_core`

### Task 3: Parameterize settings; move logging to core

**Files:** Modify `bombagif_core.py`, `main.py`, `tests/test_bombagif.py`.

**Interfaces:**
- Produces: `read_settings(token_env: str = "DISCORD_TOKEN") -> Settings` in core (same validation as today; error text becomes `f"{token_env} and ZIPLINE_TOKEN must be configured"`); `JsonFormatter`, `configure_logging(level: str) -> None` in core.
- `main._read_settings()` stays as a one-line wrapper `return read_settings("DISCORD_TOKEN")` (tests call it).

- [ ] **Step 1: Write the failing test** `test_read_settings_uses_named_token_env`: with env `{"SELFBOT_TOKEN": "t", "ZIPLINE_TOKEN": "z", "ZIPLINE_URL": "https://z.example"}` (clear=True), `read_settings("SELFBOT_TOKEN").discord_token == "t"`; and with `DISCORD_TOKEN` missing, `read_settings("SELFBOT_TOKEN")` still succeeds; with `SELFBOT_TOKEN` missing it raises `RuntimeError` whose message contains `"SELFBOT_TOKEN"`.
- [ ] **Step 2:** Run it → FAIL (`read_settings` undefined).
- [ ] **Step 3:** Implement by moving `_read_settings` body into `read_settings` and parameterizing the env name and message; move `JsonFormatter`/`configure_logging`.
- [ ] **Step 4:** Full suite → green (existing `_read_settings` tests must still pass unchanged).
- [ ] **Step 5:** Commit `Parameterize settings by token variable and move logging to core`

### Task 4: Shared `MediaSource` and `convert_and_upload`

**Files:** Modify `bombagif_core.py`, `main.py`; Test: `tests/test_core_pipeline.py`.

**Interfaces:**
- Produces: `@dataclass(frozen=True, slots=True) class MediaSource: filename: str; url: str; size: int | None = None; content_type: str | None = None` (satisfies `MediaLike`); `async def convert_and_upload(session: aiohttp.ClientSession, settings: Settings, executor: ThreadPoolExecutor, source: MediaLike, *, trace_id: str) -> str` = validate → download → convert in `executor` (video vs image path as in `_process_one_limited`) → sanitized `<stem>.gif` name → `upload_to_zipline`.
- Consumes: Task 1–3 names.

- [ ] **Step 1: Write the failing test** `test_convert_and_upload_returns_link`: patch `bombagif_core.download_attachment` (AsyncMock → a tiny valid PNG made with Pillow), `bombagif_core.upload_to_zipline` (AsyncMock → `"https://z.example/u/a.gif"`); call with `MediaSource("My Pic!.png", "https://cdn.discordapp.com/x")` and a real `ThreadPoolExecutor(1)`; assert the return value and that the upload was called with filename `"My-Pic.gif"` and bytes starting `b"GIF8"`. Second test `test_convert_and_upload_rejects_unsupported_extension`: `MediaSource("a.txt", ...)` raises `InvalidImage`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `MediaSource` and `convert_and_upload` by lifting the body of `BombagifBot._process_one_limited` (keep the `InvalidImage` passthrough and the generic-exception → `InvalidImage("That file could not be converted. ...")` wrapping and the logging fields). Then make `_process_one_limited` call `convert_and_upload(self.http_session, self.settings, self.image_executor, attachment, trace_id=trace_id)`, keeping the "still starting up" check and the semaphore in `_process_one`.
- [ ] **Step 4:** Full suite → green.
- [ ] **Step 5:** Commit `Add shared MediaSource and convert_and_upload pipeline`

---

## Phase B — Discord-free self-bot logic

### Task 5: Allowlist store

**Files:** Create `selfbot_logic.py`; Test `tests/test_selfbot_logic.py`.

**Interfaces:**
- Produces: `class AllowlistStore: __init__(self, path: Path)`, `__contains__(self, user_id: int) -> bool`, `__len__`, `add(self, user_id: int, *, now: float | None = None) -> bool` (True if newly added), `remove(self, user_id: int) -> bool`. File is JSON `{"<id>": <first_dm_epoch_seconds>}`; writes are atomic (temp file in the same dir + `os.replace`), file mode `0o600`, parent dir created.

- [ ] **Step 1: Write failing tests** (use `tempfile.TemporaryDirectory`): `test_add_persists_across_instances` (add 42, new `AllowlistStore(path)` → `42 in store`); `test_add_twice_returns_false`; `test_remove_persists`; `test_file_mode_is_600` (`stat.S_IMODE(path.stat().st_mode) == 0o600`); `test_corrupt_file_is_moved_aside` (write `"{not json"`, construct → `len(store) == 0`, `path.with_name("allowlist.json.corrupt")` exists, and an `assertLogs("bombagif.selfbot", "WARNING")` fires); `test_missing_file_is_empty`.
- [ ] **Step 2:** Run → FAIL (module missing).
- [ ] **Step 3:** Implement; logger name `bombagif.selfbot`; keys stored as strings, exposed as ints.
- [ ] **Step 4:** Run `python3 -m unittest tests.test_selfbot_logic -v` → PASS.
- [ ] **Step 5:** Commit `Add persisted DM allowlist store`

### Task 6: Rate limiter

**Files:** Modify `selfbot_logic.py`, `tests/test_selfbot_logic.py`.

**Interfaces:** Produces `class RateLimiter: __init__(self, limit: int = 5, window: float = 60.0, clock: Callable[[], float] = time.monotonic)`, `allow(self, user_id: int) -> bool` (records a hit only when allowed; sliding window, per user).

- [ ] **Step 1: Failing tests** with a fake clock list: `test_allows_up_to_limit_then_blocks` (5 × True, 6th False); `test_window_slides` (advance clock 61 s → True again); `test_users_are_independent`; `test_blocked_attempts_do_not_extend_window`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement with a per-user `deque` of timestamps. **Step 4:** PASS.
- [ ] **Step 5:** Commit `Add per-user sliding-window rate limiter`

### Task 7: Trigger decision table

**Files:** Modify `selfbot_logic.py`, `tests/test_selfbot_logic.py`.

**Interfaces:**
- Produces:
  - `class ChannelKind(enum.Enum): DM, GROUP, GUILD`
  - `@dataclass(frozen=True) class Incoming: author_id: int; author_is_bot: bool; is_own: bool; is_system: bool; channel: ChannelKind; mentions_me: bool; content: str; has_payload: bool; is_reply: bool` (`has_payload` = attachments or embeds present)
  - `class Kind(enum.Enum): IGNORE, REGISTER, UNREGISTER, CONVERT`
  - `@dataclass(frozen=True) class Decision: kind: Kind; convert: bool = False` (`convert` only meaningful for `REGISTER`: also convert this message's media)
  - `def decide(msg: Incoming, allowlist: AllowlistStore) -> Decision`
- Rules (from the spec): ignore own/bot/system. **DM + not allowlisted:** `stop` (any case, stripped) → IGNORE; else `REGISTER(convert=has_payload)`. **DM + allowlisted:** `stop` → UNREGISTER; `has_payload or is_reply` → CONVERT; else IGNORE. **GROUP/GUILD:** CONVERT only if `mentions_me` and author allowlisted; else IGNORE.

- [ ] **Step 1: Failing tests**, one per row, built with a helper `msg(**overrides)`: `test_ignores_own_bot_and_system`; `test_dm_stranger_registers`; `test_dm_stranger_with_media_registers_and_converts`; `test_dm_stranger_stop_is_ignored`; `test_dm_member_stop_unregisters` (also `" STOP "`); `test_dm_member_media_converts`; `test_dm_member_reply_converts`; `test_dm_member_plain_text_ignored`; `test_guild_mention_from_member_converts`; `test_guild_mention_from_stranger_ignored`; `test_guild_media_without_mention_ignored`; `test_group_mention_from_member_converts`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement `decide` as a flat early-return function. **Step 4:** PASS.
- [ ] **Step 5:** Commit `Add self-bot trigger decision rules`

### Task 8: Media collection from message + replied-to message

**Files:** Modify `selfbot_logic.py`, `tests/test_selfbot_logic.py`.

**Interfaces:**
- Consumes: `MediaSource`, `ALLOWED_TYPES`, `MAX_GIF_BATCH` from `bombagif_core`.
- Produces: `@dataclass(frozen=True) class Collected: sources: list[MediaSource]; skipped: int`; `def collect_media(message, referenced=None, limit: int = MAX_GIF_BATCH) -> Collected` where `message`/`referenced` are duck-typed objects with `.attachments` (items with `.filename .url .size .content_type`). Priority: `message` attachments; only if none are supported, `referenced` attachments. Unsupported extensions are dropped without counting toward `skipped`; `skipped` counts supported items beyond `limit`.

- [ ] **Step 1: Failing tests** with `types.SimpleNamespace` fakes: `test_own_attachments_win_over_referenced`; `test_falls_back_to_referenced`; `test_unsupported_files_dropped_silently` (`notes.txt` + `a.png` → one source, `skipped == 0`); `test_caps_at_limit_and_counts_skipped` (7 pngs → 5 sources, `skipped == 2`); `test_no_media_returns_empty`; `test_referenced_none_is_fine`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (suffix via `os.path.splitext(filename.lower())[1] in ALLOWED_TYPES`). **Step 4:** PASS.
- [ ] **Step 5:** Commit `Collect media from a message and its reply target`

### Task 9: Reply text

**Files:** Modify `selfbot_logic.py`, `tests/test_selfbot_logic.py`.

**Interfaces:**
- Produces: `def format_results(results: list[tuple[str, str, bool]], skipped: int = 0) -> str` (each tuple is `(filename, link_or_error, ok)`); `WELCOME_TEXT: str`; `STOP_CONFIRMATION: str`; `NO_MEDIA_TEXT: str`.
- Rules: one OK result → the bare link only. Multiple → one line each, `**name** → link` (ok) or `**name**: error` (not ok), filenames markdown-escaped (`* _ ~ | > ` \``), joined by `\n`. If `skipped > 0` append `\n(+{skipped} skipped, max 5 per message)`. Output is truncated to 2000 chars at a line boundary. `WELCOME_TEXT` explains: DM media for a GIF link, or `@` me on any message / reply to media with `@`, `stop` to opt out; ≤ 400 chars.

- [ ] **Step 1: Failing tests**: `test_single_ok_is_bare_link`; `test_multiple_lines_and_errors`; `test_filename_markdown_escaped` (`"a_b*.png"` → `a\_b\*.png`); `test_skipped_note`; `test_truncates_to_2000_at_line_boundary`; `test_welcome_mentions_stop_and_fits` (`"stop" in WELCOME_TEXT.lower()`, `len <= 400`).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Commit `Add self-bot reply formatting and copy`

---

## Phase C — The `discord.py-self` adapter

### Task 10: Self-bot packaging files

**Files:** Create `requirements-selfbot.txt`; Modify `.gitignore`, `.env.example`.

- [ ] **Step 1:** `requirements-selfbot.txt` = `requirements.txt` with `discord.py==2.7.1` replaced by `discord.py-self==2.1.0` (same pins for the rest) plus a header comment: "Self-bot only. Do NOT install into the same environment as requirements.txt."
- [ ] **Step 2:** Append `.venv-selfbot/` and `data/` to `.gitignore`. Append to `.env.example`: `SELFBOT_TOKEN=` (comment: user-account token; violates Discord ToS; use an alt account) and `SELFBOT_DATA_DIR=data`.
- [ ] **Step 3:** Build the venv and confirm it imports: `python3 -m venv .venv-selfbot && .venv-selfbot/bin/pip install -r requirements-selfbot.txt && .venv-selfbot/bin/python -c "import discord; print(discord.__version__)"` → `2.1.0`. If the system Python (3.14) is unsupported by the pin, use the newest installed Python the package supports and note it in `requirements-selfbot.txt`'s header.
- [ ] **Step 4:** Commit `Add self-bot requirements and configuration template`

### Task 11: `selfbot.py` skeleton (login, ready, shutdown)

**Files:** Create `selfbot.py`; Test `tests/test_selfbot_adapter.py`.

**Interfaces:**
- Consumes: `read_settings`, `configure_logging`, `AllowlistStore`, `MediaSource`, `convert_and_upload` and friends.
- Produces: `class SelfBot(discord.Client): __init__(self, settings: Settings, allowlist: AllowlistStore, rate_limiter: RateLimiter | None = None)` holding `self.settings`, `self.allowlist`, `self.rate_limiter`, `self.http_session`, `self.executor` (`ThreadPoolExecutor(max(1, min(2, os.cpu_count() or 1)))`), `self.semaphore` (`asyncio.Semaphore` of the same size); `setup_hook` creates the `aiohttp.ClientSession` (same timeouts/connector as `BombagifBot`); `on_ready` logs `user_id`; `close()` closes the session and executor once; `def main()` wires `read_settings("SELFBOT_TOKEN")`, `configure_logging`, `AllowlistStore(Path(os.getenv("SELFBOT_DATA_DIR", "data")) / "allowlist.json")`, signal handlers, `client.run`/`start`. Module guard `if __name__ == "__main__": main()`.
- Adapter test module skips unless `importlib.metadata.version("discord.py-self")` resolves (`unittest.skipUnless`).

- [ ] **Step 1: Failing test** `test_selfbot_constructs_and_closes` (skipped outside `.venv-selfbot`): build `SelfBot(settings, AllowlistStore(tmp/"a.json"))` with a hand-made `Settings`, call `await bot.setup_hook()`, assert `bot.http_session` is open, `await bot.close()`, assert it is closed.
- [ ] **Step 2:** Run in the venv: `.venv-selfbot/bin/python -m unittest tests.test_selfbot_adapter -v` → FAIL. **Step 3:** Implement. **Step 4:** PASS in the venv; system `python3 -m unittest discover -s tests` still green (adapter tests skipped).
- [ ] **Step 5:** Commit `Add self-bot client skeleton`

### Task 12: `on_message` → decisions → reply

**Files:** Modify `selfbot.py`, `tests/test_selfbot_adapter.py`.

**Interfaces:**
- Produces on `SelfBot`: `def _incoming(self, message) -> Incoming` (maps channel type: `discord.DMChannel` → `DM`, `discord.GroupChannel` → `GROUP`, else `GUILD`; `mentions_me = self.user in message.mentions`; `is_reply = message.reference is not None`; `has_payload = bool(message.attachments or message.embeds)`; `is_own = message.author.id == self.user.id`; `is_system = message.is_system()`); `async def on_message(self, message)`; `async def _resolve_reference(self, message) -> discord.Message | None` (cached/`resolved` first, else `await message.channel.fetch_message(id)`; returns `None` on `discord.NotFound`/`Forbidden`/`HTTPException`); `async def _convert_and_reply(self, message)`.
- Behaviour: `REGISTER` → `allowlist.add`, reply `WELCOME_TEXT`, then convert if `decision.convert`; `UNREGISTER` → `allowlist.remove`, reply `STOP_CONFIRMATION`; `CONVERT` → if `not rate_limiter.allow(author.id)` return silently; else `async with message.channel.typing()`: `collect_media(message, await self._resolve_reference(message))`; empty → reply `NO_MEDIA_TEXT`; else run each source through `convert_and_upload` under `self.semaphore` **sequentially**, `UserFacingError` → `(name, str(exc), False)`, other exceptions → log with trace id and `(name, "Could not process this file.", False)`; reply once with `format_results(results, skipped)` via `message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())`.

- [ ] **Step 1: Failing tests** (venv only; fake message objects with `AsyncMock` `reply`, patch `bombagif_selfbot`'s imported `convert_and_upload` as `selfbot.convert_and_upload`): `test_member_dm_media_replies_with_link`; `test_stranger_dm_gets_welcome_and_is_registered`; `test_stop_unregisters_and_confirms`; `test_stranger_ping_in_guild_is_silent` (`reply.assert_not_called()`); `test_rate_limited_trigger_is_silent`; `test_deleted_reference_falls_back_to_own_media` (`_resolve_reference` returns `None`); `test_user_facing_error_is_reported`; `test_reply_uses_no_mentions` (kwargs `mention_author is False`, `allowed_mentions` is `discord.AllowedMentions.none()` equivalent: all fields False).
- [ ] **Step 2:** Run in venv → FAIL. **Step 3:** Implement. **Step 4:** PASS in venv + system suite green.
- [ ] **Step 5:** Commit `Wire self-bot messages to decisions and GIF replies`

If a reviewer wants it smaller, split into two commits at the `REGISTER/UNREGISTER` boundary: first commit handles register/stop only, second adds `CONVERT`.

### Task 13: Logging context and graceful failures

**Files:** Modify `selfbot.py`, `tests/test_selfbot_adapter.py`.

- [ ] **Step 1: Failing test** `test_reply_failure_does_not_raise` (`message.reply` raises `discord.HTTPException`): `on_message` completes and logs at `ERROR`; `test_logs_do_not_contain_token` (`assertLogs` output never contains `settings.discord_token`).
- [ ] **Step 2:** Run in venv → FAIL. **Step 3:** Wrap the reply path; add `user_id`, `guild_id`, `trace_id` as `extra` (the fields `JsonFormatter` already emits). **Step 4:** PASS.
- [ ] **Step 5:** Commit `Harden self-bot replies against send failures`

---

## Phase D — Embeds, Tenor, docs

### Task 14: Embed media (Discord-proxied only)

**Files:** Modify `bombagif_core.py`, `selfbot_logic.py`, tests (`tests/test_core_pipeline.py`, `tests/test_selfbot_logic.py`), design doc.

**Interfaces:**
- Produces in core: `def is_discord_media_url(url: str) -> bool` (https, hostname equals or ends with `.discordapp.net` / `.discordapp.com` / equals `media.discordapp.net`; no credentials; port absent or 443); `def sniff_suffix(data: bytes) -> str | None` (PNG, JPEG, WEBP, AVIF via `_is_avif_container`, MP4 `ftyp`, WebM EBML `1A45DFA3` magic → matching `ALLOWED_TYPES` suffix); `convert_and_upload` uses `sniff_suffix` when the filename suffix is not in `ALLOWED_TYPES`.
- Extends `collect_media`: after attachments, look at `message.embeds` then `referenced.embeds` (same priority order as attachments). For embed types `image`, `gifv`, `video` pick `embed.video.proxy_url` (gifv/video) or `embed.image.proxy_url` / `embed.thumbnail.proxy_url` (image), require `is_discord_media_url`, derive the filename from the URL path's basename (fallback `embed.url` basename, then `"embed"`), and build `MediaSource(filename, proxy_url)`. Tenor gifv without an extension gets `.mp4`. Non-Discord hosts are dropped silently.
- Design doc amendment (this commit): the spec's "linked image URLs in content" becomes "embeds only, via Discord's proxy URLs" (SSRF-safe).

- [ ] **Step 1: Failing tests**: `test_is_discord_media_url` (accepts `https://media.discordapp.net/a.png?x=1`, `https://images-ext-1.discordapp.net/external/h/https/media.tenor.com/x/t.mp4`; rejects `http://…`, `https://evil.example/a.png`, `https://discordapp.net.evil.example/a.png`, `https://user:pw@media.discordapp.net/a.png`, `https://127.0.0.1/a.png`); `test_sniff_suffix` for each supported type's magic bytes and `None` for random bytes; `test_collect_embed_image`, `test_collect_gifv_uses_video_proxy_and_mp4_suffix`, `test_collect_drops_non_discord_embed_host`, `test_attachments_still_win_over_embeds`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Full system suite green; venv adapter suite green.
- [ ] **Step 5:** Commit `Support Discord-proxied embeds and Tenor GIFs as sources`

### Task 15: Docs

**Files:** Modify `README.md`, `CLAUDE.md`, `docs/superpowers/specs/2026-11-18-selfbot-design.md` (status line → "implemented as v2").

- [ ] **Step 1:** README: new "Self-bot (v2)" section: what it does, **ToS warning (use an alt account)**, the DM-first consent rule and `stop`, setup (`python3 -m venv .venv-selfbot`, `pip install -r requirements-selfbot.txt`, set `SELFBOT_TOKEN`/`SELFBOT_DATA_DIR`, `.venv-selfbot/bin/python selfbot.py`), why it needs a separate venv, how to run its tests, and a "manual packaging only; installer/Docker don't cover it yet" note.
- [ ] **Step 2:** `CLAUDE.md`: layout lists `bombagif_core.py`, `selfbot_logic.py`, `selfbot.py`; roadmap: v1 app (shipped), v2 self-bot (this), v3 autonomous clip scout (planned, allowlist-only, private by default); status updated.
- [ ] **Step 3:** Commit `Document the self-bot and update project context`

### Task 16: Final verification (no commit unless something is fixed)

- [ ] **Step 1:** `python3 -m unittest discover -s tests -v` (system) → all pass; `.venv-selfbot/bin/python -m unittest discover -s tests -v` → all pass (core/logic/adapter tests; app tests that need `discord.py` are expected to fail to import there — if so, confirm they are the only failures and that they pass in system python, and make `tests/test_bombagif.py` skip itself with a clear reason when `discord.py-self` is the installed distribution).
- [ ] **Step 2:** `bash tests/test_installer.sh` → pass. `git status` clean; `git log --oneline` shows one commit per task.
- [ ] **Step 3:** Live smoke test is **manual** (needs a real alt-account token): DM the account, receive the welcome; DM an image → link; ping with an image in a GC → link; reply-ping on someone's image → link; a non-DMed account pings → silence. Report this as untested by automation.
