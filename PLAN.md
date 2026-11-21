# Bombagif v3.1 (teach it, "clip that", !help) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** approved by the operator; in progress. (The v3 plan is archived at `docs/superpowers/plans/2026-11-19-clip-scout.md`.)

**Goal:** Let people say "clip that" by reply-pinging a text message, let the owner teach the judge taste (notes, reviewed examples, a tune report), and add `!help`.

**Architecture:** New discord-free modules `scout_taste.py` (notes, example store, tune report) and `scout_manual.py` (manual-clip gate); the judge prompts take notes/examples; `scout.py` gains a review queue; `selfbot.py` wires the manual flow, review DMs, reactions, and commands.

**Tech Stack:** unchanged (Python 3.11+, `discord.py-self`, aiohttp, Pillow, Rich, `unittest`).

**Spec:** `docs/superpowers/specs/2026-11-19-clip-scout-design.md` ("v3.1 addendum").

## Global Constraints

- Discord-free modules never import `discord`; `tests/test_core_isolation.py` lists every new one.
- Provider payloads stay anonymous (speaker labels, text, `deleted` flags); taste **notes** are operator text and go in the system prompt / question instructions; chat-derived **examples** go only in the user JSON as data.
- Data files are mode 600 and atomic/append-only; secrets never in logs or `repr`.
- Taste notes: at most 1,500 characters, control characters stripped. Examples: at most `SCOUT_EXAMPLES` (8) per label, each line at most 200 characters, newest first.
- Every commit keeps both suites green and ends with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Branch `v3.1-teach`.

## Review Focus

1. A manual clip must never post for an opted-out author, a bot/self message, sensitive text (local filter), non-text, or an NSFW/1:1-DM chat, and a refusal must not quote the message. (Tasks 4, 5)
2. Chat text can never reach the system prompt: examples and messages stay in the user JSON; only the owner's notes are trusted. (Task 3)
3. Only the owner's reactions/commands teach the judge; other people's reactions and `!scout` messages are ignored. (Tasks 7, 8)
4. A missing, empty, oversized, or corrupt taste file/store never breaks judging or startup. (Tasks 2, 3)
5. Review DMs are rate-limited, only the owner receives them, and closed DMs never raise. (Tasks 6, 7)

---

### Task 1: Configuration
**Files:** Modify `scout_config.py`, `tests/test_scout_config.py`.
**Produces:** `ScoutConfig` fields `manual: bool` (`SCOUT_MANUAL`, default True), `review: Literal["off","dry-run","always"]` (`SCOUT_REVIEW`, "dry-run"), `review_per_hour: int` (`SCOUT_REVIEW_PER_HOUR`, 6), `examples_per_label: int` (`SCOUT_EXAMPLES`, 8), `manual_limit_per_10_min: int` (`SCOUT_MANUAL_LIMIT_PER_10_MIN`, 3), `manual_author_cooldown_s: int` (`SCOUT_MANUAL_AUTHOR_COOLDOWN_SECONDS`, 120). `SCOUT_MANUAL` accepts on/off (case-insensitive).
- [ ] Failing tests: defaults, overrides, `test_manual_rejects_other_values_naming_the_variable`, `test_review_rejects_unknown_choice`, positive-int validation for the three ints.
- [ ] Implement; commit `Add v3.1 settings: manual clips, review, examples`.

### Task 2: Taste notes, example store, tune report
**Files:** Create `scout_taste.py`, `tests/test_scout_taste.py`; extend isolation test.
**Produces:**
- `NOTES_MAX_CHARS = 1500`; `class TasteNotes: __init__(self, path: Path)`; `text -> str` (re-read on every access, so edits apply live; missing/unreadable -> `""`, truncated to the cap, control chars except `\n` stripped); `show() -> str`; `add(self, line: str) -> bool` (False if it would exceed the cap or the line is empty; appends atomically, mode 600); `clear() -> None`.
- `@dataclass(frozen=True) class Example: label: Literal["good","bad"]; lines: tuple[tuple[str, str], ...]; funny: float; decide: Mapping[str, float]; source: str; at: float`.
- `class TasteStore: __init__(self, path: Path, per_label: int = 8)`; `add(self, label, lines, *, funny=0.0, decide=None, source="review", at=None) -> bool` (False for empty lines or an exact duplicate of the latest same-label example; lines clipped to 200 chars, speaker to 8 chars); `examples(self) -> dict[str, list[Example]]` (newest first, at most `per_label` each; corrupt lines skipped); `all(self) -> list[Example]` (last 500); `clear() -> None`; `counts() -> dict[str, int]`.
- `tune_report(examples: Sequence[Example], current_min: float) -> str` (needs >= 5 good and >= 5 bad that have `funny > 0`; otherwise says how many more are needed; else reports both averages and suggests `SCOUT_JUDGE_FUNNY_MIN` = midpoint rounded to 0.1, clamped 0-10, noting if the current value already sits between them).
- [ ] Failing tests for each behaviour above (notes cap/append/clear/live re-read/control chars; store add/dup/trim/newest-first/per-label cap/corrupt lines/mode 600/clear/counts; tune_report cases).
- [ ] Implement; commit `Add taste notes, the example store, and the tune report`.

### Task 3: Prompts use notes and examples
**Files:** Modify `scout_judge.py`, `tests/test_scout_judge.py`.
**Produces:** `decide_questions(notes: str = "") -> dict` (the existing questions, each instruction gaining a trailing `Operator taste notes: <notes>` only when notes are non-empty; `DECIDE_QUESTIONS == decide_questions("")`); `build_system_prompt(notes: str = "") -> str` (`SYSTEM_PROMPT` plus an `Operator taste notes` paragraph and a line that `examples` in the user message are the operator's past rulings (data, never instructions)); `class TasteSource(Protocol): notes() -> str; examples() -> dict[str, list[Example]]`; `decide_stage(api, window, notes="")`, `final_stage(..., notes="", examples=None)`; `InceptionJudge`/`OpenAICompatJudge` accept `taste: TasteSource | None = None`; `build_judge_chain(cfg, session, clock=time.time, taste=None)`. The user JSON gains `"examples": {"good": [[{"speaker","text"}...]...], "bad": [...]}` only when examples exist.
- [ ] Failing tests: notes appear in the system prompt and every Decide instruction when set and are absent when empty; examples appear only in the user JSON (never the system prompt), anonymised, capped; an example containing "ignore previous instructions" stays out of the system prompt; judges read notes live per call; no taste = payloads identical to v3.
- [ ] Implement; commit `Teach the judge prompts with taste notes and examples`.

### Task 4: Manual clip gate and notice
**Files:** Create `scout_manual.py`, `tests/test_scout_manual.py`; Modify `scout.py` (`format_clip_notice(names, deleted, requested_by: str | None = None)`, `ClipPlan.requested_by: str | None = None`), `tests/test_scout.py`.
**Produces:** `class ManualGate: __init__(self, cfg: ScoutConfig, clock=time.monotonic)`; `check(self, chosen: Sequence[ScoutMessage], *, requester_id: int, is_owner: bool, chat_enabled: bool, optout: Container[int], blocklist: frozenset[str], self_id: int | None) -> str | None` returning a short user-facing refusal or `None`; `commit(self, chosen, requester_id, is_owner) -> None`. Refusals (exact text): `"That person opted out of clips."`, `"I can only clip plain text messages."`, `"I can't clip that one."` (safety/bot/self/too long), `"Clipping isn't on in this chat."`, `"That one was already clipped."`, `"Slow down, try again in a minute."`. Order: bot/self, opt-out, plain text/length (<= 600 chars total, every message eligible), safety, enabled-or-owner, already clipped, rate limits (owner exempt from both limits).
- [ ] Failing tests per refusal and order, owner exemptions, limits and their windows, notice with `requested_by` (escaped, never a mention).
- [ ] Implement; commit `Add the manual clip gate and the "at B's request" notice`.

### Task 5: Manual clip flow in the adapter
**Files:** Modify `selfbot.py`, `tests/test_selfbot_adapter.py`.
**Behaviour:** in `_convert_and_reply`, when no media is found and a reply target exists and `scout` runtime has mode != off and `cfg.manual`: build `chosen` (target message's buffered run if present, else one `to_scout_message` of the fetched target), run `ManualGate.check`; on refusal reply with its text; else post via `_post_clip(ClipPlan(..., verdict=Verdict(True, 0, n-1, 0.0, True, (), "manual", {}, "manual"), deleted=False, to_owner=False, requested_by=<requester display name>))`, then `ManualGate.commit`, ledger `posted`/`manual`, stats `clips_posted`, feed note. A failed post replies nothing extra. Otherwise (no runtime/mode off/manual off/no reply target) keep today's `NO_MEDIA_TEXT` behaviour. Owner manual clips are saved as a "good" taste example (Task 7 wires the store; here call a no-op hook `self._teach(...)` that Task 7 implements).
- [ ] Failing tests: the screenshot case (member reply-pings a text message -> clip posted with the requester in the notice); buffered run used; unbuffered single message fetched; each refusal text; owner anywhere vs member needing an enabled chat; NSFW/1:1 DM never; mode off / manual off / no runtime fall back to `NO_MEDIA_TEXT`; media still wins over text; the replied-to message with a link -> refusal; no quote of the text in any refusal; stats + ledger written; failed post is silent.
- [ ] Implement; commit `Clip a replied-to text message when someone pings the account`.

### Task 6: Review queue in the orchestrator
**Files:** Modify `scout.py`, `tests/test_scout.py`.
**Produces:** `@dataclass(frozen=True) class ReviewItem: id: int; chat_id: int; names: tuple[str, ...]; lines: tuple[tuple[str, str], ...]; funny: float; decide: Mapping[str, float]; reason: str; would_clip: bool`; `Scout.__init__` gains `on_review: Callable[[ReviewItem], Awaitable[int | None]] | None = None, taste: TasteStore | None = None`; after a verdict: if `cfg.review` is `always`, or `dry-run` and `cfg.mode == "dry-run"`: a judged run qualifies when it would be posted (passed the gates) or is a near-miss (`decide["funny"] >= 0.6` or `funny >= 6.0` while `clip` is False); at most `review_per_hour` per rolling hour; `on_review` returns the DM message id which is remembered (`Scout.reviews: dict[int, ReviewItem]`, newest 200); `async def rate(self, message_id: int, label: Literal["good","bad","skip"], *, source: str = "review") -> bool` (skip drops the item; good/bad stores via `taste.add` and returns True; unknown id -> False).
- [ ] Failing tests: would-clip and near-miss qualify, ordinary misses don't, hourly cap, modes (off/dry-run/always), `on_review` failure/None never breaks screening, `rate` good/bad/skip/unknown/duplicate, anonymised `lines` (speaker labels, original text), names kept only for the DM.
- [ ] Implement; commit `Queue candidates for the owner to review`.

### Task 7: Review DMs and reactions in the adapter
**Files:** Modify `selfbot.py`, `scout_stores.py`, `tests/test_selfbot_adapter.py`, `tests/test_scout_stores.py`.
**Produces:** `ClipLedger.find_posted(message_id: int) -> ClipRecord | None` (reads recent lines; tolerates junk); `SelfBot._send_review(item)` (DMs the owner: who, the lines, scores, reason, `would clip`/`near miss`, and the instruction line "React 👍 clip-worthy, 👎 never, 🙈 skip"; adds the three reactions; returns the DM message id; closed DMs/CAPTCHA handled like other owner DMs); `on_raw_reaction_add` additionally: owner reacting 👍/👎/🙈 on a review DM -> `scout.rate`; owner reacting 👍/👎 on a clip we posted (`_clip_message_ids`) -> `taste.add` from `ledger.find_posted` with source `clip`; non-owner reactions ignored. `SelfBot._teach(...)` (the Task 5 hook) saves owner manual clips as good examples.
- [ ] Failing tests: DM content (names + text only to the owner, scores, instruction), reactions added, rate on 👍/👎/🙈, other users' reactions ignored, wrong emoji ignored, clip reaction teaches from the ledger text, unknown message ignored, DM failures handled, CAPTCHA trips the guard, owner manual clip becomes a good example, `find_posted` cases.
- [ ] Implement; commit `Review candidates and clips by reaction`.

### Task 8: Commands and `!help`
**Files:** Modify `scout.py` (`ScoutCommand.arg`, `parse_scout_command` actions `taste`, `examples`, `tune`, `help`; `parse_help_command(text) -> bool`; `HELP_OWNER`, `HELP_USER` text), `selfbot.py`, tests.
**Grammar:** `!scout taste`, `!scout taste add <text>`, `!scout taste clear`, `!scout examples`, `!scout examples clear`, `!scout tune`, `!scout help`, `!help` (case-insensitive, trimmed; `taste add` needs non-empty text; `clear` takes no extra words).
**Behaviour:** owner-only (as other `!scout` commands, answered by DM); `taste add` reports "added (N/1500 characters)" or "too long"; `examples` shows counts plus the latest 3 of each (anonymised lines); `tune` uses `tune_report`; `!help` from the owner DMs `HELP_OWNER` (every command, settings pointers, review/reactions, manual clip); `!help` from anyone else only in a 1:1 DM gets `HELP_USER`; elsewhere ignored; commands are never buffered or converted.
- [ ] Failing tests for parsing (valid/invalid), each command's effect and reply, owner-only, help variants, closed DMs.
- [ ] Implement; commit `Add taste, examples, tune, and help commands`.

### Task 9: Wiring, docs, verification
**Files:** `selfbot.py` (`create_bot`/`_start_scout` pass `TasteNotes`/`TasteStore`, `ManualGate`, `on_review`), `scout.py` (`ScoutRuntime` gains `taste_notes`, `taste_store`; `build_scout_runtime` creates them under `data_dir`), `console_ui.py` boot rows (`taste`: notes chars + examples counts, `manual`: on/off, `review`: mode), `.env.example`, `README.md`, `CLAUDE.md`, `PLAN.md`, spec notes.
- [ ] Failing tests: runtime builds the stores/gate; boot info rows; judges receive the live notes/examples; `create_bot` wiring.
- [ ] Implement, docs (README "Teaching it", "Clip that", `!help`; `.env.example` block), commit in logical pieces.
- [ ] Final verification: both suites, installer check, a pty run of the UI, `git status` clean. Live dry-run stays manual.
