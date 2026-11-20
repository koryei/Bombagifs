# Bombagif v3: the clip scout, design

Status: approved and implemented on branch `v3-scout`. Size: a project. Builds on v2.1 (self-bot, CAPTCHA guard, terminal UI).

## Intent

A "Medal for chat". The self-bot account quietly watches the chats the operator turns it on in, spots a
message (or a short run of messages) that is **genuinely funny with no context**, renders it as a static
Discord-style quote card, and posts that card back into the same chat, naming who was clipped and how to opt
out. Deleted and edited messages are still clippable, because the account keeps its own short-lived copy.

Why: friends say funny things that vanish into scrollback or get deleted. A bot that catches them is the joke.

## Decisions already made with the operator

- Anyone in an enabled chat can be clipped (no DM-first requirement for clipping). Consent is replaced by
  **disclosure + easy opt-out**: every clip names the people in it and says "DM me `stop` to opt out".
- A clip is one person's run of consecutive messages, like Discord's grouped messages. If the judge decides the
  exchange itself is the joke, it may span **up to 3 authors**. The judge ("AI critical thinking") picks the window.
- Static image only. No animation, no meme edits. The bar is "genuinely funny out of context": precision over recall.
- Deleted messages are logged and still clipped ("people think they're slick").
- Providers: Mercury Decide screens, Mercury 3 judges. The Inception API **shuts down after 2026-12-28**
  (Inception's own wind-down notice; the NVIDIA acquisition rumour is unconfirmed), so the judge is swappable.
- A stranger who pings the account (never DMed it) now gets a short help reply instead of silence (v2 ignored them).

## Goals

- Find and post genuinely funny, standalone clips from enabled chats, with an obvious who/opt-out notice.
- Catch messages that were deleted or edited after being posted.
- A `dry-run` mode that shows what it *would* clip (candidates, scores) in the terminal without posting, so the
  operator can calibrate thresholds on real chats before going live.
- Survive the Inception shutdown with an env change, not a rewrite.
- Reuse v2 pieces: `CaptchaGuard`, `RateLimiter`, `BotStats`, terminal UI, `bombagif_core` helpers.

## Non-goals (v3.0)

- Animated clips, memes, video, audio, voice. Messages with attachments, stickers, embeds, polls, or spoilers
  are not clippable.
- Learning from feedback (BombaclatAI-style weights). Thresholds are static and operator-tuned.
- Persisting chat history. The message buffer is **in memory only**.
- A web UI. Terminal + `!scout` owner commands only.
- Packaging the self-bot in Docker/installer (unchanged from v2).

## Anti-goals (failure even if it "works")

- Posting anything sensitive: personal info, credentials, self-harm, sexual content, slurs, minors, health,
  tragedy. A local filter plus the judge both block it; the judge **cannot override** the local filter.
- Posting a clip that is only funny with context, or merely mildly funny. Missing a joke is fine.
- Scouting a chat nobody enabled, a 1:1 DM, an NSFW channel, bots, or someone who opted out.
- Spamming a chat, or tripping Discord's anti-automation (CAPTCHA) with bursts.
- Leaking tokens/keys or sending names, ids, or avatars to the LLM provider.
- Pinging people with the clip notice (names are shown, not mentioned).

## Constraints

- `bombagif_core.py` and every new logic module stay **discord-free** (`discord.py` and `discord.py-self` cannot
  share an environment). Only `selfbot.py` imports `discord`.
- No inbound network port. Outbound only: Discord, Inception (or fallback provider), Zipline (unchanged), and a
  fixed emoji image host (see Rendering).
- Inception terms: submissions may be used for training **unless** "Improve the model for everyone" is turned OFF
  in the API Platform (retention period unstated). The operator must turn it off; the terminal boot banner shows a
  reminder row while the Inception judge is active. Free tier: 100M tokens is a **one-time** credit per account.
- Python 3.11+ (3.14 in use). Pillow without raqm (basic layout only, no complex-script shaping).

## Approaches considered

| | Approach | Trade-off |
|---|---|---|
| **A (chosen)** | Staged pipeline: in-memory buffer, local candidate signals, Mercury Decide screen, Mercury 3 judge, safety gates, render, post. Judge behind an interface (Inception now, OpenAI-compatible later, heuristics-only as last resort) | More modules; Decide code (one small file) is throwaway after 2026-12-28 |
| B | Mercury 3 only, no Decide | Simpler and survives the swap, but ignores the request to use Decide and sends every candidate to the larger model |
| C | Rules only, LLM later | Fast, but rules cannot tell "genuinely funny" or sensitivity; contradicts the bar |

## Architecture

```
gateway events ──> ScoutBuffer (memory, TTL) ──> candidate picker ──> Judge (Decide, then Mercury 3)
 message / edit /     versions, deleted flag,      reaction window,        returns window + scores
 delete / reaction    reaction + laugh counts      deletion fast-path           │
                                                                               v
 post (reply or send) <── ClipRenderer (Pillow PNG) <── safety gates <── Verdict (start,end,funny,...)
        │                                              cooldowns, opt-out, caps
        └──> data/clips.jsonl ledger, BotStats feed, terminal UI
```

New discord-free modules (all unit-tested without Discord):

| Module | Responsibility |
|---|---|
| `scout_buffer.py` | `ScoutMessage`/`ScoutBuffer`: per-chat rolling buffer with message versions, deleted flag, reaction/laugh counts, TTL, opt-out filtering, window extraction |
| `scout_safety.py` | Eligibility rules and the local sensitive-content filter (regexes, Luhn, token shapes) |
| `scout_judge.py` | `Judge` protocol + `InceptionJudge` (Decide + Mercury 3), `OpenAICompatJudge`, `HeuristicJudge`, provider chain with automatic fallback, call budget |
| `scout.py` | The orchestrator: scheduler loop, stage thresholds, cooldowns, opt-out store, chat-enable store, ledger |
| `clip_render.py` | Pure rendering: `ClipCard` dataclasses to PNG bytes (markdown subset, emoji, wrapping) |
| `selfbot.py` | Adapter only: maps gateway events to the above, posts the clip, handles `stop`/`start`/`!scout`, stranger help reply |

### Message buffer (in memory only)

- Per chat id (guild channel or group DM): a deque capped at `SCOUT_BUFFER_MAX_PER_CHAT` (200) entries and
  `SCOUT_BUFFER_MINUTES` (15) of age. Nothing is written to disk; a restart empties it.
- Entry: message id, chat id, author id, **display name** (guild nickname), avatar URL, role colour, server tag
  (`User.primary_guild.tag`), created time, ordered list of content **versions** with timestamps, `deleted_at`,
  laugh-reaction count, laugh-reply count, `clipped` flag.
- **Only enabled chats are buffered.** Events from any other chat (and all 1:1 DMs) are dropped on arrival, never stored.
- Events (raw, so uncached messages work; events for messages that were never buffered, e.g. sent before startup, are ignored): `on_message` appends; `on_raw_message_edit` appends a version (the
  *original* is the one judged by default, so editing the joke away does not escape); `on_raw_message_delete`
  and `on_raw_bulk_message_delete` set `deleted_at` (the buffer keeps the content); `on_raw_reaction_add/remove`
  maintain laugh-reaction counts (😂 🤣 💀 😭).
- A later message from another author containing laughter (`lmao`, `lmfao`, `dead`, 💀, 😭, `😂`) within the window
  counts as a **laugh reply** to the preceding message(s) by someone else.
- Opted-out authors' messages are never stored.

### Candidate picking

- A **candidate is a run**: consecutive eligible messages by one author within 7 minutes (the same grouping the card
  uses), capped at 8 messages / 600 characters (longer runs are split). Screening runs, not single messages, keeps
  call volume and overlapping windows down.
- Every `SCOUT_TICK_SECONDS` (5): find runs whose **newest** message has aged past the **reaction window**
  (`SCOUT_REACTION_WINDOW_SECONDS`, 45) and that were not yet screened (a run that grows is screened again once, when
  it settles). `SCOUT_SCREEN=all` (default) screens every eligible run; `signals` screens only runs with a laugh signal. (`all` is the "scout out-of-context
  lines" behaviour, so it is the default; `signals` is the privacy-lean option.)
- **Deletion fast-path:** a run containing a deleted message is screened immediately, without waiting out the
  reaction window (stricter thresholds below).
- The judge sees a **context window** around the run: up to 5 messages before and 3 after, contiguous, from the
  buffer (deleted ones included and flagged), with opted-out authors' messages **removed** (not masked) and
  ineligible messages (links, attachments) kept only as the placeholder line `[not text]` so adjacency is preserved.

### Eligibility (local, before anything leaves the machine)

All must hold: chat is enabled; chat is a guild text channel or group DM, **not** a 1:1 DM and **not** NSFW;
author is not a bot, not the account itself, not opted out; message is a normal user message (not system);
text-only (no attachments, stickers, embeds, polls, `||spoilers||`); not a bot-style command (starts with `!`,
`/`, `.` followed by a word); 4 to 400 characters after stripping; contains no URL. Messages containing a URL are
simply ineligible.

### Judge (two stages, both must agree)

Data minimisation: the provider receives **only** the window text with anonymous speaker labels (`A`, `B`, `C`),
a per-line `deleted` flag, and nothing else (no names, ids, avatars, server or channel names).

1. **Mercury Decide** (`POST /v1/decisions`, model `mercury-decide`, $0.02 per M input tokens). One call, `state` = the
   labelled window, questions: `funny` (noul), `standalone` (noul: "would a stranger find this funny with no
   surrounding chat?"), `sensitive` (noul: personal info, credentials, self-harm, sexual, slurs, minors, health,
   tragedy), `quality` (score, 5 levels). Pass thresholds (defaults, env-tunable): `funny >= 0.85`,
   `standalone >= 0.80`, `sensitive <= 0.10`.
2. **Mercury 3** (`POST /v1/chat/completions`, model `mercury-3`, `response_format: json_schema`, `strict: true`,
   `reasoning_effort: low`, temperature 0.5-1.0). Returns
   `{clip: bool, start: int, end: int, authors: int, funny: 0-10, standalone: bool, sensitive: [enum], reason: str<=160}`.
   It chooses the exact contiguous window: `start`/`end` are **0-based indexes of lines in the window it was sent**,
   inclusive. The orchestrator validates `0 <= start <= end < len(window)`, at most 8 messages, at most 600
   characters, at most 3 authors, no `[not text]` lines inside it; anything else counts as "no clip". The judge must
   decline when the joke needs context. The clipped authors (and the notice names) are the distinct authors of the
   final window, in order; the rendered text is each message's **original** version. Pass: `clip`, `standalone`, `funny >= 8`, `sensitive == []`.
- **Deleted messages** (any line in the final window deleted): `funny >= 9` and Decide `sensitive <= 0.05`,
  because people often delete things they regret sharing. `SCOUT_CLIP_DELETED=on|owner|off` (default `on`):
  `owner` sends deleted clips privately to the owner instead of the chat.
- **Untrusted text:** message text is data, never instructions. It goes in a delimited JSON field; the system
  prompt says so. A message that tries to steer the judge can at worst win one clip, and that clip still has to
  pass the local filter, opt-out, cooldowns, and caps. The judge never sees or sets policy.
- Provider chain via `SCOUT_JUDGE=auto|inception|openai|heuristic`. `auto` = Inception if `INCEPTION_API_KEY` is
  set and today is before 2026-12-28 UTC, then an OpenAI-compatible provider (`SCOUT_LLM_BASE_URL`,
  `SCOUT_LLM_API_KEY`, `SCOUT_LLM_MODEL`; must support `json_schema`) if configured, then heuristic. A provider
  disables itself (one clear log line, shown in the terminal feed) on 401/402/404/410 or 5 consecutive 5xx/timeouts
  and the next one takes over. The OpenAI-compatible judge runs one structured call combining both stages' questions.
- **Heuristic judge never auto-posts.** Without an LLM there is no humour or sensitivity judgement, so it can only
  surface candidates in the terminal (dry-run behaviour).
- Calls are bounded: shared limiter (30/min), `SCOUT_MAX_CALLS_PER_DAY` (2000), 15 s timeout, 2 retries with
  backoff on 429/503, concurrency 2. Budget exhausted = screening pauses, with a feed warning.

### Safety gates (after the judge, before rendering)

1. **Local sensitive filter** on the final window text (the judge cannot override): emails, phone numbers
   (>=9 digits), IPv4/IPv6, card-like numbers (13-19 digits passing Luhn), long hex/base64 strings (>=32),
   key-shaped tokens (`sk-`, `ghp_`, `xox[bp]-`, `AIza`, `Bearer ...`), Discord invite links, street-address
   shapes, `@everyone`/`@here`, plus a small configurable blocklist file for slurs. Any hit rejects the window.
2. Re-check opt-out for every author **immediately before posting**.
3. Never clip the same message twice; skip windows overlapping a clip already posted or pending.
4. **Cooldowns/caps:** `SCOUT_CHAT_COOLDOWN_MINUTES` (15) per chat, `SCOUT_AUTHOR_COOLDOWN_MINUTES` (120) per
   author, `SCOUT_MAX_PER_HOUR` (4) overall. A `CaptchaGuard` trip pauses the whole scout.
5. Mode: `off` (default), `dry-run` (log + terminal feed, no post), `on`.

### Posting

- Reply to the **last non-deleted message** in the window with the PNG attached
  (`discord.File(BytesIO, "clip.png")`), `mention_author=False`, `AllowedMentions.none()`. If every message in the
  window is deleted (or the reply fails with "unknown message"), use `channel.send` instead.
- Notice text: ``🎬 Clipped **Pranit** — DM me `stop` to opt out of clips.`` For several people:
  ``🎬 Clipped **Pranit**, **Shan** and **Lochan** — DM me `stop` to opt out of clips.`` Names are the display names
  used on the card, markdown-escaped, bold, never mentions. If any message was deleted, append
  ` (deleted, but not fast enough)` (my call; wording is the builder's).
- Record the posted message id (also in the ledger, reloaded for the last 500 clips at startup, so it survives a
  restart) so a reply of exactly `stop` to it opts that replier out.
- Captcha or HTTP errors while posting follow the v2 rules (`CaptchaGuard` trips; other errors are logged).

### Opt-out, enabling chats, and stranger pings

- **`data/optout.json`** (mode 600, atomic): user ids who opted out of clipping.
- **`stop`** (case-insensitive, exact) in a **1:1 DM**, or as a **reply to a clip** in any chat, adds the author to
  `optout.json` and, if they were allowlisted for conversions, removes them (v2 behaviour). Confirmation text says
  clipping *and* conversions are off. **`start`** in a DM removes the opt-out. A later plain DM re-registers them for
  conversions only, and the confirmation says clipping stays off until `start`.
- **Enabling chats:** the owner (`SELFBOT_OWNER_ID`, required to use `!scout`) sends `!scout on` / `!scout off`
  in a chat (or `!scout on <chat id>` in the owner's DM), `!scout status`, `!scout list`. State lives in
  `data/scout-chats.json`. `SELFBOT_SCOUT_CHATS` (comma-separated ids) seeds it. Nobody else's `!scout` does
  anything. **Scouting is off everywhere by default**; with mode `dry-run`/`on` and no enabled chats the scout idles
  and the terminal says so. Turning a chat on **posts a one-time announcement there** ("I'm clipping messages that
  are funny out of context in this chat, naming whoever's in them; DM me `stop` to opt out") so nobody is surprised;
  `!scout off` posts a short "stopped clipping here". Both replies are skipped in `dry-run` (the owner gets the
  confirmation only).
- When `SELFBOT_CLIP_DELETED=owner` applies, the owner gets a DM with the PNG, the notice text, and a jump link to the
  chat; nothing is posted publicly.
- **Stranger pings** (direct mention by someone not on the allowlist, in a guild or group DM): one help reply:
  ask them to DM "hi" first (and add the account if DMs are closed), mention clips + `stop` if the scout is on.
  Throttled to once per person per hour and once per chat per 10 minutes; ignored when the author is a bot, the
  account is CAPTCHA-paused, or the author opted out.
- The v2 **welcome text** gains one line about clipping (and `stop`) whenever scout mode is not `off`.

### Rendering (`clip_render.py`)

Static PNG, a faithful Discord-style card matching the operator's reference screenshot (dark, avatar left, name +
server tag + time, one line per message). Reference colours sampled from it: background `#040404`, tag chip
`#202223`, timestamp grey `#949ba4`, text `#dbdee1`, names the member's role colour (default white). Theme
constant `CLIP_THEME=onyx|dark` (`dark` = Discord's `#313338`).

- Logical width 520 px, rendered at 2x (1040 px) for sharp text; padding 16; avatar 40 px circle; name 16 px
  semibold; timestamp 12 px, local time of the host, format `7:09 PM`; body 16 px with 22 px line height;
  consecutive messages from one author within 7 minutes group under one header (Discord behaviour); a new author
  starts a new header. Max height 900 logical px (the 8-message/600-char window cap keeps it under).
- **No reply header, no channel/server name, no jump link**: the clip is deliberately context-free.
- Markdown subset: `**bold**`, `*italic*`, `__underline__`, `~~strike~~`, `` `code` ``, fenced code blocks;
  everything else is plain text. `<@id>`/`<@!id>` render as `@Name` pills (name from the buffer/guild cache,
  else `@unknown`), `<#id>` as `#channel`, `<@&id>` as `@role`, `<t:...>` as plain text.
- Emoji: custom `<:name:id>`/`<a:name:id>` fetch `cdn.discordapp.com/emojis/<id>.png` (animated shows frame 1);
  unicode emoji use Twemoji 72x72 PNGs from a **fixed** host (`cdn.jsdelivr.net`, pinned version), cached in
  `data/emoji-cache/`. Fetch failure falls back to the text glyph. Both go through allowlisted-host checks
  (`is_discord_media_url` for Discord; an explicit constant for the emoji host).
- Avatar: `display_avatar` at size 128 via `download_url` + `is_discord_media_url`; failure shows a coloured
  circle with the author's initial. Fonts: Noto Sans (Regular/Medium/Bold/Italic) and Noto Sans Mono bundled in
  `assets/fonts/` with their OFL licence; glyph fallback is the builder's call (Pillow has no raqm).
- Pure function: `render_clip(card: ClipCard, *, scale: int = 2, theme: str = "onyx") -> bytes`. No network inside
  the pure part; fetched assets (avatars, emoji) are passed in via a resolver so tests stay offline.

### Persistence and logging

- `data/clips.jsonl` (mode 600, append-only): time, chat id, message ids, author ids, scores, deleted flag,
  provider, mode (`posted`/`dry-run`/`owner`), and the clipped **text only for posted/owner clips** (it is public
  anyway). Rejected candidates are logged without text.
- Terminal UI: the boot banner gains `scout` (mode), `judge` (active provider, plus "Inception: confirm training
  opt-out is OFF" while it is active), `scout chats` (count); the live panel gains counters (screened, passed
  Decide, clips posted, deleted rescued, dry-run hits, provider state) and the feed shows candidates with scores in
  `dry-run`. Pretty log lines for scout events; JSON stays the default off a TTY.

### Configuration summary (all optional unless marked)

`SELFBOT_SCOUT_MODE=off|dry-run|on` (off) · `SELFBOT_OWNER_ID` (required for scout) · `SELFBOT_SCOUT_CHATS` ·
`SCOUT_JUDGE=auto|inception|openai|heuristic` (auto) · `INCEPTION_API_KEY` · `SCOUT_LLM_BASE_URL/API_KEY/MODEL` ·
`SCOUT_SCREEN=all|signals` (all) · thresholds `SCOUT_DECIDE_FUNNY_MIN` (0.85), `SCOUT_DECIDE_STANDALONE_MIN`
(0.80), `SCOUT_DECIDE_SENSITIVE_MAX` (0.10), `SCOUT_JUDGE_FUNNY_MIN` (8) · `SELFBOT_CLIP_DELETED=on|owner|off`
(on) · cooldowns/caps above · `SCOUT_BUFFER_MINUTES` (15) · `SCOUT_REACTION_WINDOW_SECONDS` (45) ·
`SCOUT_MAX_CALLS_PER_DAY` (2000) · `CLIP_THEME` (onyx). Every default is documented in `.env.example`.

## Testing

Offline and deterministic, in the existing `unittest` style:

- Buffer: versions, edit keeps original, delete keeps content, TTL, cap, opt-out never stored, window extraction.
- Safety: each regex class with positives and near-miss negatives (Luhn, 9-digit rule, key shapes); eligibility table.
- Judge clients against a local `aiohttp` mock server (Decide and chat shapes from Inception's docs): thresholds,
  deleted stricter bar, schema validation (invalid or out-of-range output means **no clip**), 401/404/5xx fallback
  chain, daily budget, anonymised payload (assert no names/ids are sent), prompt-injection text cannot bypass gates.
- Orchestrator with a fake judge and fake clock: reaction window, deletion fast-path, cooldowns, caps, dry-run posts
  nothing, opt-out re-check before post, no double clipping, CAPTCHA pause.
- Renderer: wrapping, grouping, markdown subset, emoji tokenising, theme colours at known pixels, dimensions and
  height cap; no network (resolver faked). One real-render smoke test writes a PNG and checks it decodes.
- Adapter (venv only): events map to buffer updates; `stop`/`start`/`!scout` handling and owner-only enforcement;
  stranger help throttling; reply-vs-send fallback; notice text with 1/2/3 names and the deleted suffix.
- A pty + screen-emulator check of the terminal UI is run manually, as for v2.2.1.

## Build order (atomic commits)

1. Buffer, 2. safety filter, 3. opt-out + chat stores, 4. judge interface + heuristic, 5. Decide client,
6. Mercury 3 client + provider chain, 7. orchestrator (dry-run first), 8. renderer (layout, then markdown, then
emoji/avatars), 9. adapter events, 10. posting + `stop`/`start`/`!scout`, 11. stranger help, 12. terminal UI rows,
13. docs. `dry-run` ships before `on` is reachable.

## Left to the builder

Exact prompt wording; the blocklist file format; font fallback strategy; precise Decide `quality` level labels;
the laughter-word list; ledger rotation; how to pin the Twemoji version; welcome/help copy (within the stated content).

## My calls (not discussed; easy to change)

- Scouting is **off everywhere** until the owner enables a chat; screening defaults to `all` messages in enabled
  chats; `stop` also removes conversions; `start` re-enables clipping.
- Names in the notice are not mentions; the notice is appended with "(deleted, but not fast enough)" for deleted.
- Static PNG attachment, no Zipline hop; thresholds (0.85 / 0.80 / 0.10 / 8, deleted 9 and 0.05) are starting points
  to calibrate in `dry-run`.
- Heuristic-only mode never auto-posts; deleted clips default to the chat (`on`), with `owner` as the safety valve.
- Timestamp uses the host's local time zone.

## Risks the operator should know

- **Privacy:** enabled chats' messages are sent (anonymised, text only) to a third party (Inception, then whichever
  provider replaces it). Turn off Inception's training toggle. Tell the people in the chats.
- **ToS and detection:** a user-account bot that reads everything and posts uninvited is easier for Discord to flag
  than v2. Caps, cooldowns, and the CAPTCHA guard reduce but do not remove this.
- **Judgement quality:** thresholds are untested on real chats; `dry-run` exists to fix that before `on`.
- **Shutdown:** after 2026-12-28 the Inception judge is gone; confirm the replacement provider supports
  `json_schema` before then.

## Implementation notes (where the build differs from this design)

- **Welcome/help clipping line only when mode is `on`.** In `dry-run` nothing is ever posted, so telling people "I clip
  you" would be false; the chat announcement is skipped in `dry-run` for the same reason (spec said "not `off`").
- **`!scout` answers go to the owner by DM** (status, list, refusals), so commands add no public noise. With scout mode
  `off` but `SELFBOT_OWNER_ID` set the command is refused with a reason; with no scout configured it is ignored.
- **A well-formed but invalid judge reply is a no-clip verdict**, not a provider failure, so deterministic garbage is
  never retried. Provider failures (timeouts, 5xx, non-JSON) return "no verdict" and the run is retried after 30 s.
- **Dry-run mimics `on`:** it consumes cooldowns and the hourly cap and flags messages as clipped, so dry-run counts
  match what live posting would do. Owner-routed (deleted) clips count only as `deleted_rescued`.
- **Chat-completions base URL:** a `SCOUT_LLM_BASE_URL` ending in `/v1` (the OpenAI SDK convention) posts to
  `/chat/completions`; a bare host posts to `/v1/chat/completions`.
- **Phone rule is 9-15 digits** (E.164 maximum) so 16-19 digit numbers that fail the card checksum (Discord ids) are
  allowed; Luhn-valid 13-19 digit runs are blocked as cards.
- **Extras:** `clip_assets.py` (asset host allowlist + card building, discord-free), `ScoutBuffer.drop_chat`,
  `NotoSans-BoldItalic`, an `emoji` package pin with a regex fallback when it is missing, `read_settings(env=...)`,
  `create_bot(...)`, and a `python clip_render.py demo` preview.
- **Not yet verified live:** real Mercury 3 / Decide behaviour, threshold calibration, posting in a real chat.
